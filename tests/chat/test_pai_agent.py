"""聊天 Agent 端到端验证（FunctionModel 驱动，不打真实模型）。

覆盖聊天 Agent 的核心行为：
- 压缩的确定性 / 幂等 / 配对保持（压缩落库方案的前提）
- `SessionStore` append-only 存储 + 压缩事件落库
- **恢复一致性**（压缩事件可重放的前提）
- `run_stream_events` 的事件分流与中断

`compaction.py` 与 `session_store.py` 均位于 `kanade_bot/plugins/chat/agent/`。
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import aclosing, asynccontextmanager, closing
from datetime import datetime, time
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

# localstore 根目录指向临时目录：chat/config.py 模块级初始化会读取（缺失时写出）
# 默认配置文件，避免落在仓库或用户真实数据目录
_LOCALSTORE_TMP = Path(tempfile.mkdtemp(prefix="test-pai-agent-localstore-"))
os.environ.setdefault("LOCALSTORE_CONFIG_DIR", str(_LOCALSTORE_TMP / "config"))
os.environ.setdefault("LOCALSTORE_CACHE_DIR", str(_LOCALSTORE_TMP / "cache"))
os.environ.setdefault("LOCALSTORE_DATA_DIR", str(_LOCALSTORE_TMP / "data"))

REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 初始化 NoneBot，供被测链路里任何触及 nonebot 全局单例（get_driver / logger / config）的地方使用。
import nonebot

nonebot.init()

# `kanade_bot/plugins/chat/__init__.py` 会连带导入 handler，把整个 nonebot 插件运行时
# 拉进来（get_driver、localstore 配置目录、require("model_updater") 等），单测里起不来。
# 这里把该包登记成一个只声明子模块路径、不执行 __init__ 的壳，
# 使 `kanade_bot.plugins.chat.agent.*` 能按路径直接导入。
_chat_pkg = types.ModuleType("kanade_bot.plugins.chat")
_chat_pkg.__path__ = [str(REPO_ROOT / "kanade_bot" / "plugins" / "chat")]  # type: ignore[attr-defined]
sys.modules.setdefault("kanade_bot.plugins.chat", _chat_pkg)

# `chat/config.py`（被 session_store / compaction 传递导入）require 了 model_updater。
# 必须经 PluginManager 正式加载而非直接 import：model_updater 内部使用 localstore 的
# 目录函数，后者靠栈帧回溯 `__nonebot_plugin__` 定位调用方插件。
nonebot.load_plugin("kanade_bot.plugins.model_updater")

# chat 包是壳模块、不是正式插件，chat/config.py 模块级初始化（configs_file_path）里
# localstore 的调用方探测会失败。把调用方固定为已正式加载的 model_updater
# （localstore 目录已指向临时目录，无副作用）。
import nonebot_plugin_localstore as _localstore

_localstore._try_get_caller_plugin = lambda: nonebot.get_plugin("model_updater")  # type: ignore[assignment]

# 主动回复包同理做壳：其 __init__ 导入 runtime → chat.util → require("crystal")，
# 单测环境起不来。deliver / manager 重依赖一并桩掉，被测子模块按路径直接导入。
_proactive_pkg = types.ModuleType("kanade_bot.plugins.chat.agent.proactive")
_proactive_pkg.__path__ = [  # type: ignore[attr-defined]
    str(REPO_ROOT / "kanade_bot" / "plugins" / "chat" / "agent" / "proactive")
]
sys.modules.setdefault("kanade_bot.plugins.chat.agent.proactive", _proactive_pkg)
_deliver_stub = types.ModuleType("kanade_bot.plugins.chat.deliver")
_deliver_stub.send_proactive = None  # type: ignore[attr-defined]
sys.modules.setdefault("kanade_bot.plugins.chat.deliver", _deliver_stub)
_manager_stub = types.ModuleType("kanade_bot.plugins.chat.agent.manager")
_manager_stub.chat_manager = SimpleNamespace()  # type: ignore[attr-defined]
sys.modules.setdefault("kanade_bot.plugins.chat.agent.manager", _manager_stub)

from pydantic_ai import Agent
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
    repair_messages,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage, UsageLimits
from pydantic_ai_harness.compaction import (
    compact_now,
    estimate_context_tokens,
    estimate_token_count,
)

from kanade_bot.plugins.chat.agent import history as compaction_mod
from kanade_bot.plugins.chat.agent.history.store import SessionStore
from kanade_bot.plugins.chat.agent.proactive import executor as proactive_executor
from kanade_bot.plugins.chat.agent.proactive.gate_history import (
    SessionGateHistory,
    daily_reset_boundary,
)
from kanade_bot.plugins.chat.agent.session import ChatSession
from kanade_bot.plugins.chat.agent.session import stats as session_mod
from kanade_bot.plugins.chat.config import CompactionConfig as CompactionParams
from kanade_bot.utils import pai_runtime
from kanade_bot.utils.schema import BaseAgentConfig, ProviderConfig
from kanade_bot.utils.session import SessionInfo


class RequestBudgetTest(unittest.IsolatedAsyncioTestCase):
    def test_image_estimate_uses_count_not_binary_size(self):
        small = BinaryContent(data=b"x", media_type="image/jpeg")
        large = BinaryContent(data=b"x" * 10_000_000, media_type="image/png")

        small_messages = [ModelRequest(parts=[UserPromptPart(content=[small])])]
        large_messages = [ModelRequest(parts=[UserPromptPart(content=[large])])]

        self.assertEqual(
            compaction_mod.estimate_request_tokens(small_messages),
            compaction_mod.IMAGE_TOKEN_ESTIMATE,
        )
        self.assertEqual(
            compaction_mod.estimate_request_tokens(large_messages),
            compaction_mod.IMAGE_TOKEN_ESTIMATE,
        )
        self.assertEqual(
            compaction_mod.estimate_request_tokens(small_messages, image_tokens_per_image=512),
            512,
        )

    def test_only_images_after_usage_anchor_are_added(self):
        image = BinaryContent(data=b"x", media_type="image/jpeg")
        messages = [
            ModelRequest(parts=[UserPromptPart(content=[image])]),
            ModelResponse(
                parts=[TextPart(content="done")],
                usage=RequestUsage(input_tokens=500, output_tokens=10),
            ),
            ModelRequest(parts=[UserPromptPart(content=[image])]),
        ]

        self.assertEqual(
            compaction_mod.estimate_request_tokens(messages),
            510 + compaction_mod.IMAGE_TOKEN_ESTIMATE,
        )

    async def test_over_budget_request_is_rejected_before_model_call(self):
        model, scripted = _make_model([resp_text("should not run")])
        agent = Agent(
            model,
            capabilities=[
                compaction_mod.RequestBudgetCapability(
                    context_window=100_000,
                    output_upper_tokens=65_536,
                )
            ],
        )
        with self.assertRaises(compaction_mod.ContextBudgetError):
            await agent.run("x" * 400_000)
        self.assertEqual(scripted.sent_messages, [])

    async def test_incident_sized_request_reduces_completion_budget(self):
        received: list[int | None] = []

        def answer(_messages, info):
            received.append(info.model_settings.get("max_tokens") if info.model_settings else None)
            return resp_text("ok")

        model = FunctionModel(answer)
        agent = Agent(
            model,
            capabilities=[
                compaction_mod.RequestBudgetCapability(
                    context_window=1_048_576,
                    output_upper_tokens=393_216,
                )
            ],
        )
        await agent.run("x" * (659_139 * 4))

        # 1,048,576 - 659,139 - ceil(5%) = 337,008.
        self.assertEqual(received, [337_008])

    async def test_summary_agent_uses_inherited_dynamic_settings(self):
        received: list[int | None] = []

        def answer(_messages, info):
            received.append(info.model_settings.get("max_tokens") if info.model_settings else None)
            return resp_text("summary")

        model = FunctionModel(answer)
        params = CompactionParams(
            context_window=30_000,
            summary_keep_messages=1,
            summary_keep_user_messages=False,
        )
        await compact_now(
            compaction_mod.build_summary(
                params,
                model_settings={"max_tokens": 25_000},
                output_upper_tokens=25_000,
            ),
            make_history(20),
            model=model,
        )

        self.assertEqual(len(received), 1)
        self.assertGreaterEqual(received[0], compaction_mod.MIN_OUTPUT_TOKENS)
        self.assertLess(received[0], 25_000)


CLEARED = "[tool result cleared]"

TEST_PARAMS = CompactionParams(
    trigger_fraction=0.5,
    # context_window 压到很小，让测试历史必然触发压缩；
    # 生产用 trigger_fraction 相对模型真实窗口，不设这一项。
    context_window=200,
    keep_pairs=1,
    # 清理收益至少 1 个 token 才动手，相当于「不设阈值」
    min_clear_tokens=1,
    # 应急阀同样压低，让 run 内压缩在测试中必然触发。
    # 注意 FunctionModel 会合成 usage 锚点，锚定估算远小于纯启发式，
    # 阈值不压低的话在线压缩在测试里永远不会触发（生产默认 0.98）
    online_valve_fraction=0.5,
)
"""测试用压缩参数：窗口小 ⇒ 必然触发；keep_pairs=1 便于断言"""


# ===== 构造消息 =====


def make_history(n_pairs: int, tag: str = "old") -> list[ModelMessage]:
    """构造带工具调用对的历史（每轮：用户提问 → 带工具调用的响应 → 工具结果）"""
    msgs: list[ModelMessage] = []
    for i in range(n_pairs):
        msgs.append(ModelRequest(parts=[UserPromptPart(content=f"{tag}q{i}")]))
        msgs.append(
            ModelResponse(
                parts=[
                    TextPart(content=f"{tag}a{i}"),
                    ToolCallPart(tool_name="t", args={"i": i}, tool_call_id=f"{tag}c{i}"),
                ],
                finish_reason="stop",
            )
        )
        msgs.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="t",
                        content=f"{tag}result{i}" * 80,
                        tool_call_id=f"{tag}c{i}",
                    )
                ]
            )
        )
    return msgs


def resp_text(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)], finish_reason="stop")


def resp_tool(name: str, args: dict, call_id: str) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name=name, args=args, tool_call_id=call_id)],
        finish_reason="stop",
    )


def resp_truncated(text: str) -> ModelResponse:
    """模拟 max_output_tokens 截断"""
    return ModelResponse(parts=[TextPart(content=text)], finish_reason="length")


def fp(messages) -> bytes:
    """归一化后的消息指纹

    Pydantic AI 在每次请求前都会跑 `repair_messages()`（合并相邻请求、
    补全孤儿工具结果），所以比较历史必须先归一化，否则会看到假差异。
    """
    return ModelMessagesTypeAdapter.dump_json(repair_messages(list(messages)))


def _uncleared(messages) -> set[str]:
    """尚未被清理的工具结果 id 集合"""
    return {
        p.tool_call_id
        for m in messages
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart) and p.content != CLEARED
    }


# ===== 脚本化模型 =====


class ScriptedModel:
    """按脚本返回响应序列，并记录每次实际发送的消息

    非流式与流式共用同一份 `sent_messages`，因此两种路径可对比。
    """

    def __init__(self, script: list[ModelResponse]):
        self.script = script
        self.index = 0
        self.sent: list[bytes] = []
        self.sent_messages: list[list[ModelMessage]] = []

    def _next(self, messages) -> ModelResponse:
        normalized = repair_messages(list(messages))
        self.sent.append(ModelMessagesTypeAdapter.dump_json(normalized))
        self.sent_messages.append(normalized)
        response = self.script[min(self.index, len(self.script) - 1)]
        self.index += 1
        return response

    async def __call__(self, messages, info: AgentInfo) -> ModelResponse:
        return self._next(messages)

    def stream(self, messages, info: AgentInfo):
        """流式：`FunctionModel.stream_function` 产出文本增量或工具调用增量

        官方明确要求**同一次响应内不要混吐文本与工具调用**，故按内容二选一。
        """
        response = self._next(messages)
        tool_parts = [p for p in response.parts if isinstance(p, ToolCallPart)]

        if tool_parts:

            async def gen_tools():
                for part in tool_parts:
                    yield {
                        0: DeltaToolCall(
                            name=part.tool_name,
                            json_args=json.dumps(part.args_as_dict(), ensure_ascii=False),
                            tool_call_id=part.tool_call_id,
                        )
                    }

            return gen_tools()

        text = "".join(p.content for p in response.parts if isinstance(p, TextPart))

        async def gen_text():
            for chunk in _chunks(text):
                yield chunk

        return gen_text()


def _chunks(text: str, size: int = 8) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)] or [""]


def _make_model(script: list[ModelResponse]) -> tuple[FunctionModel, ScriptedModel]:
    scripted = ScriptedModel(script)
    return FunctionModel(scripted, stream_function=scripted.stream), scripted


def build_agent(
    script: list[ModelResponse], recorder=None, *, params: CompactionParams | None = None
) -> tuple[Agent, ScriptedModel]:
    """构造与 manager 同构的 Agent（FunctionModel 驱动）"""
    p = params or TEST_PARAMS
    recorder = recorder or compaction_mod.build_compaction_capability(p)
    model, scripted = _make_model(script)

    agent: Agent = Agent(
        model,
        capabilities=[recorder, PrepareTools(lambda ctx, defs: defs)],
    )

    @agent.tool_plain
    def t(i: int) -> str:
        """测试工具"""
        return f"tool-out-{i}" * 40

    return agent, scripted


DEPS_C1 = SimpleNamespace(session_info=SimpleNamespace(session_id="c1"))
"""模拟 ChatDeps：让 RecordingCompaction 能识别会话"""


# ===== 压缩的确定性（落库方案的前提） =====


class CompactionDeterminismTest(unittest.IsolatedAsyncioTestCase):
    """`ClearToolResults` 的确定性 + 幂等"""

    async def test_replay_equals_online(self):
        """从原始全量重放 == 在线压缩结果（逐字节）"""
        history = make_history(4)
        mark = compaction_mod.build_clear_mark(TEST_PARAMS)

        first = await compaction_mod.apply_strategy(history, mark)
        second = await compaction_mod.apply_strategy(first, mark)
        third = await compaction_mod.apply_strategy(history, mark)

        self.assertEqual(fp(first), fp(third), "相同输入+参数 ⇒ 相同输出")
        self.assertEqual(fp(first), fp(second), "重复应用应幂等")

    async def test_keeps_message_positions(self):
        """就地清空内容而非删消息——消息结构与位置不变（前缀缓存仍命中）"""
        history = make_history(4)
        result = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(TEST_PARAMS)
        )
        self.assertEqual(len(result), len(history))
        self.assertLess(_uncleared(result), _uncleared(history))

    async def test_keep_pairs_changes_result(self):
        """参数漂移确实会改变结果（所以必须把参数记进 mark）"""
        history = make_history(4)
        a = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(CompactionParams(keep_pairs=1))
        )
        b = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(CompactionParams(keep_pairs=3))
        )
        self.assertNotEqual(fp(a), fp(b))

    async def test_tool_pairing_preserved(self):
        """压缩后工具调用/返回仍然配对（provider 会拒绝孤儿）"""
        result = await compaction_mod.apply_strategy(
            make_history(4), compaction_mod.build_clear_mark(TEST_PARAMS)
        )
        calls = {
            p.tool_call_id
            for m in result
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolCallPart)
        }
        returns = {
            p.tool_call_id
            for m in result
            for p in getattr(m, "parts", [])
            if isinstance(p, ToolReturnPart)
        }
        self.assertEqual(calls, returns, "不应出现孤儿 call/return")


# ===== SessionStore =====


class SessionStoreTest(unittest.IsolatedAsyncioTestCase):
    """append-only 存储 + 压缩事件落库"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    async def test_append_and_load_roundtrip(self):
        history = make_history(3)
        await self.store.append("c1", history)
        self.assertEqual(fp(await self.store.load("c1")), fp(history))
        self.assertEqual(await self.store.count("c1"), len(history))

    async def test_conv_id_isolated(self):
        await self.store.append("c1", make_history(2, "a"))
        await self.store.append("c2", make_history(3, "b"))
        self.assertEqual(await self.store.count("c1"), 6)
        self.assertEqual(await self.store.count("c2"), 9)

    async def test_db_keeps_uncompacted_messages(self):
        """核心要求：DB 里被压缩掉的消息仍然完整保留"""
        history = make_history(4)
        await self.store.append("c1", history)
        mark = compaction_mod.build_clear_mark(TEST_PARAMS)
        mark.up_to_seq = len(history) - 1
        await self.store.add_compaction_mark("c1", mark)

        restored = await self.store.restore("c1", params=TEST_PARAMS)
        self.assertEqual(len(restored), len(history), "消息条数应保持不变")
        self.assertLess(_uncleared(restored), _uncleared(history), "旧工具结果应已清空")
        self.assertEqual(await self.store.count("c1"), len(history), "DB 保留全量")
        self.assertEqual(fp(await self.store.load("c1")), fp(history))

    async def test_rollback_removes_tail_keeps_earlier_marks(self):
        await self.store.append("c1", make_history(4))  # 12 行，seq 0..11
        mark = compaction_mod.build_clear_mark(TEST_PARAMS)
        mark.up_to_seq = 11
        await self.store.add_compaction_mark("c1", mark)
        await self.store.append("c1", make_history(2, "new"))  # seq 12..17

        await self.store.rollback("c1", 12)
        self.assertEqual(await self.store.count("c1"), 12, "只回退 seq>=12 的尾部")
        marks = await self.store.load_compaction_marks("c1")
        self.assertEqual(len(marks), 1, "up_to_seq<12 的 mark 应保留")

    async def test_rollback_removes_marks_created_this_turn(self):
        """本轮产生的 mark（up_to_seq 越过回退点）应连同补写的消息一并删除

        真实编排中 mark 记录前总会先补写本轮尚未落库的消息（至少含
        user 消息），故本轮 mark 的 up_to_seq 必然 >= 本轮起始行数。
        """
        await self.store.append("c1", make_history(4))  # 上轮已存在 seq 0..11
        db_floor = await self.store.count("c1")  # = 12

        # 本轮：压缩前补写 user 消息（seq 12），再记 mark
        await self.store.append("c1", [ModelRequest(parts=[UserPromptPart(content="q")])])
        mark = compaction_mod.build_clear_mark(TEST_PARAMS)
        mark.up_to_seq = await self.store.count("c1") - 1  # = 12
        await self.store.add_compaction_mark("c1", mark)

        await self.store.rollback("c1", db_floor)
        self.assertEqual(await self.store.count("c1"), 12, "回退到本轮起始行数")
        self.assertEqual(len(await self.store.load_compaction_marks("c1")), 0, "本轮 mark 应删除")

    async def test_clear_removes_everything(self):
        await self.store.append("c1", make_history(2))
        await self.store.add_compaction_mark("c1", compaction_mod.build_clear_mark(TEST_PARAMS))
        await self.store.clear("c1")
        self.assertEqual(await self.store.count("c1"), 0)
        self.assertEqual(await self.store.load("c1"), [])


# ===== 图片外置与还原（blob 存储） =====


def make_image_history(tag: str = "img") -> list[ModelMessage]:
    """带 BinaryContent 图片的用户消息 + 响应"""
    image = BinaryContent(data=b"\x89PNG-fake-image-bytes" + tag.encode(), media_type="image/png")
    return [
        ModelRequest(parts=[UserPromptPart(content=[f"{tag}看这张图", image])]),
        ModelResponse(parts=[TextPart(content=f"{tag}好的")], finish_reason="stop"),
    ]


class BlobExternalizationTest(unittest.IsolatedAsyncioTestCase):
    """BinaryContent 外置为 blobs 表 + 占位符，读取边界无损还原"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "sessions"
        self.store = SessionStore(self.root)

    def _first_payload(self, conv_id: str = "c1") -> bytes:
        with sqlite3.connect(self.root / f"{conv_id}.sqlite3") as conn:
            return conn.execute("SELECT payload FROM messages ORDER BY seq LIMIT 1").fetchone()[0]

    async def test_roundtrip_dedup_and_placeholder_payload(self):
        history = make_image_history()
        await self.store.append("c1", history)
        await self.store.append("c1", history[:1])  # 同图重发

        restored = await self.store.load("c1")
        self.assertEqual(fp(restored[:2]), fp(history), "图片消息应无损还原")

        payload = self._first_payload()
        self.assertIn(b"$kanade-blob:", payload, "payload 内应为占位符")
        self.assertNotIn(b"\x89PNG-fake-image-bytes", payload, "原始 bytes 不得入 JSON")
        with sqlite3.connect(self.root / "c1.sqlite3") as conn:
            blobs = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
        self.assertEqual(blobs, 1, "同图去重后只存一份")

    async def test_summary_mark_result_externalized(self):
        """摘要快照里的图片同样外置，恢复直接用快照且还原无损"""
        history = make_image_history()
        await self.store.append("c1", history)
        mark = compaction_mod.build_summary_mark(TEST_PARAMS, history)
        mark.up_to_seq = len(history) - 1
        await self.store.add_compaction_mark("c1", mark)

        marks = await self.store.load_compaction_marks("c1")
        self.assertEqual(
            fp(ModelMessagesTypeAdapter.validate_json(marks[0].result)),
            fp(history),
            "mark result 读取时应已还原为真实 BinaryContent",
        )
        restored = await self.store.restore("c1", params=TEST_PARAMS)
        self.assertEqual(fp(restored), fp(history), "summarizing mark 整体替换后应等于快照")


class RestoreCacheTest(unittest.IsolatedAsyncioTestCase):
    """restore 结果缓存：命中免重放、写路径失效、参数指纹隔离"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    async def test_cache_hit_invalidate_on_write(self):
        history = make_history(2)
        await self.store.append("c1", history)

        first = await self.store.restore("c1", params=TEST_PARAMS)
        db = self.store.registry.get("c1")
        self.assertIsNotNone(db._restore_cache, "restore 后应写入缓存")
        second = await self.store.restore("c1", params=TEST_PARAMS)
        self.assertEqual(fp(first), fp(second))

        await self.store.append("c1", make_history(1, "new"))
        self.assertIsNone(db._restore_cache, "写路径应失效缓存")
        third = await self.store.restore("c1", params=TEST_PARAMS)
        self.assertEqual(len(third), len(history) + 3)

    async def test_cache_keyed_by_params_fingerprint(self):
        await self.store.append("c1", make_history(2))
        other = CompactionParams(keep_pairs=TEST_PARAMS.keep_pairs + 7)

        await self.store.restore("c1", params=TEST_PARAMS)
        db = self.store.registry.get("c1")
        cached = db._restore_cache
        await self.store.restore("c1", params=other)
        self.assertIsNot(db._restore_cache, cached, "参数不同不得命中旧缓存")


class FingerprintScopeTest(unittest.TestCase):
    """参数指纹只覆盖重放相关字段：生成期字段变更不触发漂移，重放字段变更必触发"""

    def test_generation_fields_excluded(self):
        """生成期字段变化不影响指纹"""
        tweaked = CompactionParams(
            **{
                **TEST_PARAMS.model_dump(),
                "summary_prompt_file": "Other.md",
                "summary_model": "some-model",
                "summary_keep_messages": 99,
                "summary_keep_user_messages": False,
                "summary_target_fraction": 0.9,
                "min_clear_tokens": None,
                "online_valve_fraction": None,
            }
        )
        self.assertEqual(TEST_PARAMS.fingerprint(), tweaked.fingerprint())

    def test_replay_fields_included(self):
        """重放相关字段变化必须改变指纹"""
        for field, value in (
            ("trigger_fraction", 0.1),
            ("keep_pairs", 42),
            ("context_window", 99999),
        ):
            with self.subTest(field=field):
                tweaked = CompactionParams(**{**TEST_PARAMS.model_dump(), field: value})
                self.assertNotEqual(TEST_PARAMS.fingerprint(), tweaked.fingerprint())


# ===== 恢复一致性（压缩事件可重放的前提） =====


class RestoreConsistencyTest(unittest.IsolatedAsyncioTestCase):
    """恢复的历史必须与关闭前完全一致"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "sessions"

    async def test_restored_matches_online_history(self):
        """记录「关闭前实际发送的消息指纹」→ 重启 → 重建 → 指纹必须相同"""
        store = SessionStore(self.db)

        # ---- 关闭前：跑一轮（工具调用 + 收尾），期间触发压缩 ----
        history = make_history(4)
        await store.append("c1", history)

        recorder = compaction_mod.build_compaction_capability(TEST_PARAMS)
        agent, scripted = build_agent([resp_tool("t", {"i": 1}, "n1"), resp_text("done")], recorder)

        result = await agent.run("问题A", message_history=history, deps=DEPS_C1)
        # 与 manager 一致的持久化编排：补写压缩前消息 + 记 mark + 尾部消息
        await compaction_mod.persist_run(
            store,
            "c1",
            recorder,
            base=history,
            all_messages=result.all_messages(),
            new_messages=result.new_messages(),
        )

        final_history = repair_messages(result.all_messages())
        online_final = fp(final_history)
        last_request = scripted.sent_messages[-1]

        # ---- 重启：新 store 实例，从 DB 恢复 ----
        reborn = SessionStore(self.db)
        restored = await reborn.restore("c1", params=TEST_PARAMS)

        # 断言 1：恢复的历史 == 关闭前的完整历史
        self.assertEqual(fp(restored), online_final, "恢复的历史必须与关闭前完全一致")

        # 断言 2：关闭前最后一次请求发送的内容 == 历史去掉最后一条响应
        # （那条响应正是这次请求的产物，还没进入下一次请求）
        self.assertEqual(
            fp(last_request),
            fp(final_history[:-1]),
            "最后一次请求的内容应等于历史去掉最后一条响应",
        )

        # 断言 3：重启后首次请求 = 恢复的历史 + 新提问，历史部分逐字节一致
        # （这是保护 provider 侧 KV Cache 的关键）
        next_agent, _ = build_agent([resp_text("下一轮")])
        await next_agent.run("问题B", message_history=restored)
        next_request = next_agent._model.function.sent_messages[0]
        self.assertEqual(fp(next_request[:-1]), online_final, "重启后首次请求的历史应与关闭前一致")
        self.assertEqual(
            str(next_request[-1].parts[0].content),
            "问题B",
            "末尾应是本轮新增的用户提问",
        )

    async def test_param_drift_discards_old_marks(self):
        """改 keep_pairs 后恢复：丢弃旧 marks 并按新参数重放，结果自洽"""
        store = SessionStore(self.db)
        history = make_history(4)
        await store.append("c1", history)
        mark = compaction_mod.build_clear_mark(CompactionParams(keep_pairs=1))
        mark.up_to_seq = len(history) - 1
        await store.add_compaction_mark("c1", mark)

        new_params = CompactionParams(keep_pairs=3)
        reborn = SessionStore(self.db)
        restored = await reborn.restore("c1", params=new_params)

        expected = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(new_params)
        )
        self.assertEqual(fp(restored), fp(expected))
        marks = await reborn.load_compaction_marks("c1")
        self.assertEqual(len(marks), 1, "漂移重放有实际效果时应落一条覆盖全量的新 mark")
        self.assertEqual(marks[0].strategy, "clear_tool_results")
        self.assertEqual(marks[0].up_to_seq, len(history) - 1)

    async def test_summarizing_mark_uses_stored_result(self):
        """摘要档非确定性：恢复时直接用存下的产物，不重新生成"""
        store = SessionStore(self.db)
        history = make_history(3)
        snapshot = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(TEST_PARAMS)
        )
        await store.append("c1", history)
        await store.add_compaction_mark(
            "c1",
            compaction_mod.CompactionMark(
                strategy="summarizing",
                params=TEST_PARAMS.model_dump(),
                result=compaction_mod.compaction.messages_fingerprint(snapshot),
                fingerprint=TEST_PARAMS.fingerprint(),
                applied_at=0.0,
            ),
        )

        reborn = SessionStore(self.db)
        restored = await reborn.restore("c1", params=TEST_PARAMS)
        self.assertEqual(fp(restored), fp(snapshot))

    async def test_messages_after_summarizing_mark_survive_restore(self):
        """生产事故回归：summarizing mark 之后追加的消息不能在恢复时被丢弃

        旧版 restore 对摘要 mark 做整体替换，mark 之后每轮新追加的消息全部
        丢失——模型对压缩后发生的对话逐轮失忆（表现为重复搜索已查过的内容）。
        """
        store = SessionStore(self.db)
        history = make_history(3)
        await store.append("c1", history)

        # 摘要压缩：快照落 mark，up_to_seq = 压缩时的全量行数-1
        params = CompactionParams(summary_keep_messages=3)
        summary_model, _ = _make_model([resp_text("这是摘要")])
        compacted = await compact_now(
            compaction_mod.build_summary(params), history, model=summary_model
        )
        self.assertIsNot(compacted, history)
        mark = compaction_mod.build_summary_mark(params, compacted)
        mark.up_to_seq = len(history) - 1
        await store.add_compaction_mark("c1", mark)

        # mark 之后又聊了一轮（9 条新消息）
        later = make_history(3, "later")
        await store.append("c1", later)

        restored = await SessionStore(self.db).restore("c1", params=params)
        self.assertEqual(
            fp(restored),
            fp([*compacted, *later]),
            "恢复结果应为摘要快照 + mark 之后追加的消息",
        )

    async def test_multi_turn_with_repeated_compaction(self):
        """多轮对话 + 中途多次压缩 + 重启：恢复历史与在线一致

        生产事故的完整形态：压缩后继续聊多轮，期间再次压缩，
        重启后恢复的历史必须等于最后一次请求发送的内容。
        """
        store = SessionStore(self.db)
        recorder = compaction_mod.build_compaction_capability(TEST_PARAMS)
        history: list[ModelMessage] = []
        await store.append("c1", history)

        # 5 轮对话，每轮带工具调用；窗口 200 token ⇒ 每轮都触发压缩
        for turn in range(5):
            agent, _ = build_agent(
                [resp_tool("t", {"i": turn}, f"n{turn}"), resp_text(f"第{turn}轮回答")],
                recorder,
            )
            result = await agent.run(f"第{turn}轮问题", message_history=history, deps=DEPS_C1)
            all_msgs = result.all_messages()
            await compaction_mod.persist_run(
                store,
                "c1",
                recorder,
                base=history,
                all_messages=all_msgs,
                new_messages=result.new_messages(),
            )
            history = all_msgs

        marks = await store.load_compaction_marks("c1")
        self.assertGreaterEqual(len(marks), 1, "应至少记录一次压缩")

        # 重启恢复：与最后一轮在线历史（repaired 形态）逐字节一致
        restored = await SessionStore(self.db).restore("c1", params=TEST_PARAMS)
        self.assertEqual(fp(restored), fp(repair_messages(history)), "恢复历史与在线一致")

        # 再来一轮：恢复后的历史继续可用，且包含最后一轮的回答
        self.assertTrue(
            any(
                isinstance(p, TextPart) and p.content == "第4轮回答"
                for m in restored
                for p in getattr(m, "parts", [])
            ),
            "恢复的历史应包含最后一轮的回答",
        )


# ===== 主模型窗口解析（压缩阈值固化） =====


class ResolveModelContextWindowTest(unittest.TestCase):
    """`CompactionConfig.context_window` 未显式时从 pai_runtime 解析主模型窗口

    生产事故教训：窗口解析失败会静默回退 200k，压缩阈值从 0.9×1M 变成
    0.9×200k。解析链：显式配置 > /models 元数据缓存 > genai-prices。
    """

    def test_explicit_config_wins(self):
        """显式配置的 context_window 优先，不触发元数据获取"""
        cfg = BaseAgentConfig(
            model="m",
            provider=ProviderConfig(base_url="http://x"),
            context_window=1234,
        )
        self.assertEqual(pai_runtime.resolve_model_context_window(cfg), 1234)

    def test_metadata_cache_hit(self):
        """无显式配置时复用 /models 元数据缓存（如 deepseek-flash 的 1M）"""
        provider = ProviderConfig(base_url="http://x")
        cfg = BaseAgentConfig(model="m", provider=provider)
        key = (pai_runtime._client_key(provider), "m")
        saved = pai_runtime._models_metadata_cache.get(key)
        try:
            pai_runtime._models_metadata_cache[key] = (1048576, 393216)
            self.assertEqual(pai_runtime.resolve_model_context_window(cfg), 1048576)
        finally:
            if saved is None:
                pai_runtime._models_metadata_cache.pop(key, None)
            else:
                pai_runtime._models_metadata_cache[key] = saved

    def test_unresolvable_returns_none(self):
        """解析全部失败时返回 None（调用方保持未固化状态）"""
        provider = ProviderConfig(base_url="http://unreachable.invalid")
        cfg = BaseAgentConfig(model="m", provider=provider)
        key = (pai_runtime._client_key(provider), "m")
        saved = pai_runtime._models_metadata_cache.get(key)
        try:
            pai_runtime._models_metadata_cache.pop(key, None)  # 强制重新 fetch → 失败
            fake_model = mock.MagicMock()
            fake_model.context_window = None
            fake_model.model_id = "m"
            with mock.patch.object(pai_runtime, "get_model", return_value=fake_model):
                # registry 无该模型 → resolve_context_window 返回 None
                self.assertIsNone(pai_runtime.resolve_model_context_window(cfg))
        finally:
            if saved is None:
                pai_runtime._models_metadata_cache.pop(key, None)
            else:
                pai_runtime._models_metadata_cache[key] = saved


# ===== 手动总结级压缩 =====


class ManualCompactionTest(unittest.IsolatedAsyncioTestCase):
    """手动 LLM 摘要压缩：`compact_now` + `build_summary` / `build_summary_mark`"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "sessions"

    async def test_manual_compact_record_and_restore(self):
        """手动压缩 → mark 落库 → 重启恢复与压缩产物一致，DB 全量保留"""
        store = SessionStore(self.db)
        history = make_history(6)
        await store.append("c1", history)

        params = CompactionParams(summary_keep_messages=5)
        summary_model, _ = _make_model([resp_text("这是摘要")])
        compacted = await compact_now(
            compaction_mod.build_summary(params),
            history,
            model=summary_model,
            conversation_id="c1",
        )
        self.assertIsNot(compacted, history, "历史超过保留尾部时应发生摘要压缩")
        self.assertLess(len(compacted), len(history))
        self.assertEqual(compaction_mod.extract_summary(compacted), "这是摘要")

        mark = compaction_mod.build_summary_mark(params, compacted)
        mark.up_to_seq = len(history) - 1
        self.assertEqual(mark.strategy, "summarizing")
        self.assertIsNotNone(mark.result)
        await store.add_compaction_mark("c1", mark)

        reborn = SessionStore(self.db)
        self.assertEqual(fp(await reborn.restore("c1", params=params)), fp(compacted))
        self.assertEqual(await reborn.count("c1"), len(history), "DB 仍全量保留")

    async def test_manual_compact_noop_within_keep_tail(self):
        """历史不超过保留尾部时为无操作，原样返回同一列表"""
        history = make_history(1)
        summary_model, _ = _make_model([resp_text("这是摘要")])
        result = await compact_now(
            compaction_mod.build_summary(CompactionParams(summary_keep_messages=20)),
            history,
            model=summary_model,
        )
        self.assertIs(result, history)
        self.assertIsNone(compaction_mod.extract_summary(result))


# ===== 压缩后上下文估算（2026-10-27 生产事故修复） =====


class PostCompactionEstimateTest(unittest.IsolatedAsyncioTestCase):
    """压缩后估算：usage 锚点位于保留尾部内时，直接估算不体现回收（196558→196558 事故）"""

    @staticmethod
    def _anchored_history() -> list[ModelMessage]:
        """真实形态历史：末轮响应携带 usage，其 input_tokens 覆盖全部前文"""
        history = make_history(4)  # 12 条，响应均无 usage
        history.append(ModelRequest(parts=[UserPromptPart(content="final q")]))
        history.append(
            ModelResponse(
                parts=[TextPart(content="final a")],
                usage=RequestUsage(input_tokens=90_000, output_tokens=500),
                finish_reason="stop",
            )
        )
        return history

    async def test_summary_reclaim_reflected_in_post_estimate(self):
        """摘要压缩后：锚定基线减去启发式口径的回收量（与 TieredCompaction 补偿一致）"""
        history = self._anchored_history()
        params = CompactionParams(summary_keep_messages=3)
        summary_model, _ = _make_model([resp_text("这是摘要")])
        compacted = await compact_now(
            compaction_mod.build_summary(params),
            history,
            model=summary_model,
            conversation_id="c1",
        )
        self.assertLess(len(compacted), len(history))

        est_before = estimate_context_tokens(history)
        self.assertEqual(est_before, 90_500, "锚点 = input+output，覆盖全部前文")
        # 事故复现：锚点响应位于保留尾部内，其 usage 度量的是压缩前的请求，
        # 压缩改写对锚点不可见 → 直接对产物估算与压缩前完全相同
        self.assertEqual(estimate_context_tokens(compacted), est_before)

        est_after = compaction_mod.estimate_post_compaction_tokens(history, compacted)
        reclaimed = estimate_token_count(history) - estimate_token_count(compacted)
        self.assertGreater(reclaimed, 0)
        self.assertEqual(est_after, est_before - reclaimed)
        self.assertLess(est_after, est_before)

    async def test_without_anchor_equals_direct_estimate(self):
        """无 usage 锚点（纯启发式）时：补偿估算 == 对产物的直接启发式估算"""
        history = make_history(4)
        params = CompactionParams(summary_keep_messages=3)
        summary_model, _ = _make_model([resp_text("这是摘要")])
        compacted = await compact_now(
            compaction_mod.build_summary(params),
            history,
            model=summary_model,
            conversation_id="c1",
        )
        self.assertLess(len(compacted), len(history))
        self.assertEqual(
            compaction_mod.estimate_post_compaction_tokens(history, compacted),
            estimate_context_tokens(compacted),
        )


# ===== 摘要档用户消息保留（2026-10-09 生产事故修复） =====


class SummaryKeepUserMessagesTest(unittest.IsolatedAsyncioTestCase):
    """`build_summary`：保留最近用户消息、不复活会话首条用户消息

    生产事故（2026-10-09，群 875082051）：upstream `SummarizingCompaction`
    默认 `preserve_first_user_message=True`，keep_messages=0 压缩后历史只剩
    「摘要 + 会话第一条用户消息」，模型复读两天前的首条消息并按其内嵌
    时间戳错判时间（半夜回复「…喵喵。」「现在已经快到中午了呢」）。

    harness 现行语义（与早期断言的差异）：

    - `keep_messages` 的 0 也保留最新一条（工具配对安全截止），产物不再是
      「仅摘要一条」，而是「摘要 + 尾部工具配对」；
    - `keep_user_messages` 的保留物是带 `kept-user-message` metadata 的
      `TextContent` 副本（非原样 `str` content）。
    """

    async def _compact(self, history, params):
        summary_model, _ = _make_model([resp_text("这是摘要")])
        return await compact_now(compaction_mod.build_summary(params), history, model=summary_model)

    def _user_texts(self, compacted) -> list[str]:
        """产物中的用户消息文本（兼容 TextContent 副本形态）"""
        texts: list[str] = []
        for m in compacted:
            for p in getattr(m, "parts", []):
                if not isinstance(p, UserPromptPart):
                    continue
                if isinstance(p.content, str):
                    texts.append(p.content)
                else:
                    texts.append("".join(getattr(x, "content", "") for x in p.content))
        return texts

    async def test_no_first_user_message_resurrection(self):
        """事故回归：keep_messages=0 时任何用户消息不得复活（含首条 oldq0）"""
        compacted = await self._compact(
            make_history(3),
            CompactionParams(summary_keep_messages=0, summary_keep_user_messages=False),
        )
        # 摘要 + 最新一轮的工具配对尾部（响应与工具结果），无用户消息
        self.assertEqual(len(compacted), 3, "keep_messages=0 = 摘要 + 安全截止尾部")
        self.assertEqual(compaction_mod.extract_summary(compacted), "这是摘要")
        self.assertFalse(
            any(isinstance(p, UserPromptPart) for m in compacted for p in getattr(m, "parts", [])),
            "不应复活任何用户消息（事故中首条『喵喵』消息被原样保留）",
        )

    async def test_coupled_budget_zero_keeps_no_user_messages(self):
        """耦合语义：keep_messages=0 时保留用户消息的名额同样为 0"""
        compacted = await self._compact(
            make_history(3),
            CompactionParams(summary_keep_messages=0, summary_keep_user_messages=True),
        )
        # 与 keep_user_messages=False 的产物完全一致：预算 0 不保留任何用户消息副本
        self.assertEqual(len(compacted), 3)
        self.assertEqual(self._user_texts(compacted), [])

    async def test_keeps_recent_user_messages_within_budget(self):
        """保留用户消息：被摘要范围内最近 N 条用户消息以副本形态保留"""
        history = make_history(6)  # 18 条：u0..u5 共 6 条用户消息
        compacted = await self._compact(
            history,
            CompactionParams(summary_keep_messages=4, summary_keep_user_messages=True),
        )
        user_texts = self._user_texts(compacted)
        # 被摘要范围内的用户消息优先占满全部 4 个名额 → 原始尾部被挤空，
        # 产物 = 摘要 + 最近的 q1..q4 副本 + 尾部安全截止（upstream 耦合预算语义）
        self.assertEqual(user_texts, ["oldq1", "oldq2", "oldq3", "oldq4"])


# ===== 轮末自动压缩 =====


class TurnEndCompactionTest(unittest.IsolatedAsyncioTestCase):
    """`compact_between_turns`：轮末单闸（clear→summary 升级）+ mark 落库"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "sessions"

    async def test_below_gate_noop(self):
        """估算低于闸门时不压缩、不落 mark"""
        store = SessionStore(self.db)
        history = make_history(2)
        await store.append("c1", history)
        params = CompactionParams(
            trigger_fraction=0.8,
            context_window=10**9,
            summary_target_fraction=0.9,
        )
        model, _ = _make_model([resp_text("这是摘要")])
        self.assertIsNone(
            await compaction_mod.compact_between_turns(store, "c1", params, history, model=model)
        )
        self.assertEqual(await store.load_compaction_marks("c1"), [])

    async def test_unfixed_window_skips(self):
        """窗口未固化（context_window=None）时保守跳过"""
        store = SessionStore(self.db)
        params = CompactionParams(trigger_fraction=0.8, summary_target_fraction=0.9)
        model, _ = _make_model([resp_text("这是摘要")])
        self.assertIsNone(
            await compaction_mod.compact_between_turns(
                store, "c1", params, make_history(2), model=model
            )
        )

    async def test_clear_tier_records_replayable_mark(self):
        """仅零成本档生效：落可重放 clear mark，restore 重放结果一致"""
        store = SessionStore(self.db)
        history = make_history(3)  # 约 614 tokens（纯启发式，无 usage 锚点）
        await store.append("c1", history)
        # 摘要档未启用：闸门 = trigger_fraction（0.8×700=560 < 614 → 触发清理）
        params = CompactionParams(
            trigger_fraction=0.8,
            context_window=700,
            keep_pairs=1,
            min_clear_tokens=1,
        )
        model, _ = _make_model([resp_text("这是摘要")])
        info = await compaction_mod.compact_between_turns(store, "c1", params, history, model=model)
        self.assertIsNotNone(info)
        self.assertEqual(info["strategy"], "clear_tool_results")
        marks = await store.load_compaction_marks("c1")
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0].strategy, "clear_tool_results")
        self.assertIsNone(marks[0].result, "零成本档 mark 只存参数，可重放")
        self.assertEqual(marks[0].up_to_seq, len(history) - 1)
        restored = await SessionStore(self.db).restore("c1", params=params)
        expected = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(params)
        )
        self.assertEqual(fp(restored), fp(expected), "恢复重放应重现轮末压缩结果")

    async def test_summary_tier_records_snapshot_mark(self):
        """零成本档无事可做时升摘要档：落快照 mark，恢复直接用快照"""
        store = SessionStore(self.db)
        history = make_history(3)
        await store.append("c1", history)
        # keep_pairs=3 → 3 对全在保护内，clear 无事可做 → 直接升摘要
        params = CompactionParams(
            trigger_fraction=0.8,
            context_window=600,  # 0.9×600=540 < 614 → 过闸
            keep_pairs=3,
            min_clear_tokens=1,
            summary_target_fraction=0.9,
            summary_keep_messages=2,
        )
        model, _ = _make_model([resp_text("这是摘要")])
        info = await compaction_mod.compact_between_turns(store, "c1", params, history, model=model)
        self.assertIsNotNone(info)
        self.assertEqual(info["strategy"], "summarizing")
        marks = await store.load_compaction_marks("c1")
        self.assertEqual(marks[0].strategy, "summarizing")
        self.assertIsNotNone(marks[0].result)
        restored = await SessionStore(self.db).restore("c1", params=params)
        compacted = ModelMessagesTypeAdapter.validate_json(marks[0].result)
        self.assertEqual(fp(restored), fp(compacted))

    async def test_anchored_history_reports_reclaimed_estimate(self):
        """锚点位于保留尾部内：tokens_after 须体现回收而非与压缩前相同（196558→196558 事故）"""
        store = SessionStore(self.db)
        history = make_history(4)
        history.append(ModelRequest(parts=[UserPromptPart(content="final q")]))
        history.append(
            ModelResponse(
                parts=[TextPart(content="final a")],
                usage=RequestUsage(input_tokens=90_000, output_tokens=500),
                finish_reason="stop",
            )
        )
        await store.append("c1", history)
        # 0.9×100_000=90_000 < 90_500 → 过闸；keep_pairs=3 保护后 3 对 →
        # 清理档回收不足 → 升摘要档（锚点响应落在摘要档保留尾部内）
        params = CompactionParams(
            trigger_fraction=0.8,
            context_window=100_000,
            keep_pairs=3,
            min_clear_tokens=1,
            summary_target_fraction=0.9,
            summary_keep_messages=2,
        )
        model, _ = _make_model([resp_text("这是摘要")])
        info = await compaction_mod.compact_between_turns(store, "c1", params, history, model=model)
        self.assertIsNotNone(info)
        self.assertEqual(info["strategy"], "summarizing")
        self.assertEqual(info["tokens_before"], 90_500)
        self.assertLess(
            info["tokens_after"],
            info["tokens_before"],
            "压缩后估算须体现回收，而非与压缩前相同",
        )


# ===== 在线应急阀 =====


class OnlineValveTest(unittest.IsolatedAsyncioTestCase):
    """在线压缩仅作应急阀：超过高阈值触发 / None 关闭"""

    async def test_valve_disabled_never_compacts(self):
        """online_valve_fraction=None：run 内完全不压缩"""
        params = CompactionParams(
            trigger_fraction=0.5,
            context_window=200,
            keep_pairs=1,
            min_clear_tokens=1,
            online_valve_fraction=None,
        )
        recorder = compaction_mod.build_compaction_capability(params)
        agent, _ = build_agent([resp_text("ok")], recorder, params=params)
        history = make_history(3)  # 远超 trigger_fraction，但在线压缩已关闭
        result = await agent.run("问题", message_history=history, deps=DEPS_C1)
        self.assertEqual(recorder.take_events("c1"), [])
        self.assertFalse(
            any(
                p.content == CLEARED
                for m in result.all_messages()
                for p in getattr(m, "parts", [])
                if isinstance(p, ToolReturnPart)
            ),
            "在线压缩关闭时工具结果应原样保留",
        )

    async def test_valve_fires_over_threshold(self):
        """估算超过应急阈值时 run 内压缩并记录事件"""
        params = CompactionParams(
            trigger_fraction=0.5,
            context_window=200,
            keep_pairs=1,
            min_clear_tokens=1,
            online_valve_fraction=0.5,
        )
        recorder = compaction_mod.build_compaction_capability(params)
        agent, _ = build_agent([resp_text("ok")], recorder, params=params)
        await agent.run("问题", message_history=make_history(3), deps=DEPS_C1)
        self.assertTrue(recorder.take_events("c1"), "超过应急阈值应记录压缩事件")


# ===== 流式 =====


class StreamingTest(unittest.IsolatedAsyncioTestCase):
    """`run_stream_events` 的事件分流与中断"""

    async def test_text_and_tool_events(self):
        agent, _ = build_agent([resp_tool("t", {"i": 1}, "n1"), resp_text("最终回答")])

        texts: list[str] = []
        tool_called = False
        async with agent.run_stream_events(
            "问题", usage_limits=UsageLimits(request_limit=10)
        ) as stream:
            async for event in stream:
                if type(event).__name__ == "PartEndEvent":
                    part = event.part
                    if type(part) is TextPart and part.content.strip():
                        texts.append(part.content)
                elif type(event).__name__ == "FunctionToolCallEvent":
                    tool_called = True

        self.assertTrue(tool_called, "应识别到工具调用事件")
        self.assertIn("最终回答", texts)

    async def test_truncation_detected_by_finish_reason(self):
        """`finish_reason == 'length'` 原生即可判定截断"""
        agent, _ = build_agent([resp_truncated("半截内容")])
        result = await agent.run("问题")
        self.assertEqual(result.all_messages()[-1].finish_reason, "length")

    async def test_cancel_stops_run(self):
        """中断：cancel 后 run 停止（迭代时抛 RunCancelled），不产出结果"""
        from pydantic_ai.exceptions import RunCancelled

        agent, _ = build_agent([resp_text("never")])

        with self.assertRaises(RunCancelled):
            async with agent.run_stream_events("问题") as stream:
                await stream.__anext__()
                stream.cancel()
                async for _ in stream:
                    pass

        self.assertIsNone(stream.result, "取消后不应有结果")


# ===== 会话统计：平均缓存命中率 =====


class UsageScriptedModel(FunctionModel):
    """按脚本返回响应序列，并在流式路径注入脚本指定的 usage

    FunctionModel 的流式实现用 `_estimate_usage` 自行估算 usage，完全丢弃
    脚本响应 `ModelResponse.usage` 里的显式值；这里在流交付给消费方之前，
    用脚本条目配对的 usage 替换估算值，使 `stream.usage` 反映设定。
    每次流式请求消耗一个脚本条目（与 `ScriptedModel` 的推进规则一致）。
    """

    def __init__(self, script: list[tuple[ModelResponse, RequestUsage]]):
        scripted = ScriptedModel([response for response, _ in script])
        super().__init__(scripted, stream_function=scripted.stream)
        self._usages = [usage for _, usage in script]
        self._stream_index = 0

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        async with super().request_stream(*args, **kwargs) as response:
            response._usage = self._usages[min(self._stream_index, len(self._usages) - 1)]
            self._stream_index += 1
            yield response


def resp_usage(
    text: str, *, input_tokens: int, cache_read_tokens: int
) -> tuple[ModelResponse, RequestUsage]:
    """构造「文本响应 + 指定 usage」的脚本条目"""
    return (
        resp_text(text),
        RequestUsage(
            input_tokens=input_tokens, cache_read_tokens=cache_read_tokens, output_tokens=10
        ),
    )


STATS_SESSION_INFO = SessionInfo(session_id="c1", nickname="测试者", user_id="u1")
"""缓存命中率测试用的发送者信息"""

NOOP_PARAMS = CompactionParams(
    trigger_fraction=0.8,
    context_window=10**9,
    summary_target_fraction=0.9,
)
"""窗口极大：轮末压缩必然不触发，隔离出纯 usage 累计行为"""


class SessionCacheStatsTest(unittest.IsolatedAsyncioTestCase):
    """`ChatSession` 平均缓存命中率：会话级累计（Token加权），内存态、不落库"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    def _make_session(self, script: list[tuple[ModelResponse, RequestUsage]]) -> ChatSession:
        """构造与 manager 同构的最小 ChatSession（无沙箱/记忆/MCP）"""
        recorder = compaction_mod.build_compaction_capability(NOOP_PARAMS)
        agent = Agent(
            UsageScriptedModel(script),
            capabilities=[recorder, PrepareTools(lambda ctx, defs: defs)],
        )
        mgr = SimpleNamespace(
            store=self.store,
            agent=agent,
            compaction=recorder,
            compaction_params=NOOP_PARAMS,
            model_settings={},
            output_upper_tokens=None,
            mcp_toolsets=[],
            sandbox_manager=None,
            memory_store=None,
        )
        return ChatSession("c1", manager=mgr)

    async def _turn(self, session: ChatSession, prompt: str) -> None:
        async for _ in session.send_and_wait(STATS_SESSION_INFO, prompt, bot_id="b1"):
            pass

    async def test_token_weighted_hit_rate(self):
        """命中率 = 会话累计 cache_read / 累计输入 token（跨轮累计）"""
        session = self._make_session(
            [
                resp_usage("第一轮回答", input_tokens=1000, cache_read_tokens=800),
                resp_usage("第二轮回答", input_tokens=500, cache_read_tokens=500),
            ]
        )
        await self._turn(session, "问题1")
        await self._turn(session, "问题2")

        stats = await session.stats()
        assert stats is not None
        self.assertAlmostEqual(stats["cache"]["hit_rate"], 1300 / 1500)
        self.assertEqual(stats["cache"]["cache_read_tokens"], 1300)
        self.assertEqual(stats["cache"]["input_tokens"], 1500)

    async def test_reset_clears_counters(self):
        """重置会话后累计清零、命中率重新计算"""
        session = self._make_session(
            [
                resp_usage("第一轮回答", input_tokens=1000, cache_read_tokens=800),
                resp_usage("重置后回答", input_tokens=400, cache_read_tokens=100),
            ]
        )
        await self._turn(session, "问题1")
        await session.reset()
        await self._turn(session, "问题2")

        stats = await session.stats()
        assert stats is not None
        self.assertAlmostEqual(stats["cache"]["hit_rate"], 100 / 400)

    async def test_no_usage_no_cache_entry(self):
        """未产生任何模型用量时不输出 cache 字段"""
        session = self._make_session([])
        # 仅落历史、不运行模型：内存累计仍为零
        await self.store.append("c1", make_history(1))
        stats = await session.stats()
        assert stats is not None
        self.assertNotIn("cache", stats)


# ===== 主动回复防重判（2026-10-27 双回复竞态修复） =====


class ProactiveBufferGuardTest(unittest.IsolatedAsyncioTestCase):
    """`require_buffer`：主动回复轮在会话锁内复检 buffer，防被动轮消费后重复接话

    生产竞态：主动回复执行器外层 buffer 检查通过后、真正取得会话锁前，被动轮
    （to_me 触发）恰好消费了同一批消息——此时 system_notification 会使空内容轮
    照常运行，模型对着空缓冲区再接一次话 → 对同一批消息连回两次。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SessionStore(Path(self.tmp.name) / "sessions")

    def _make_session(self, script: list[ModelResponse]) -> ChatSession:
        """与 SessionCacheStatsTest 同构的最小 ChatSession（无沙箱/记忆/MCP）"""
        model, _ = _make_model(script)
        recorder = compaction_mod.build_compaction_capability(NOOP_PARAMS)
        agent = Agent(model, capabilities=[recorder])
        mgr = SimpleNamespace(
            store=self.store,
            agent=agent,
            compaction=recorder,
            compaction_params=NOOP_PARAMS,
            model_settings={},
            output_upper_tokens=None,
            mcp_toolsets=[],
            sandbox_manager=None,
            memory_store=None,
        )
        return ChatSession("c1", manager=mgr)

    async def _collect(self, gen) -> list[str]:
        out: list[str] = []
        async with aclosing(gen):
            async for content in gen:
                out.append(content)
        return out

    async def test_require_buffer_empty_aborts(self):
        """锁内复检 buffer 为空 → 直接放弃：不调模型、不落库（被动轮已消费的场合）"""
        session = self._make_session([resp_text("不应产生")])
        out = await self._collect(
            session.send_and_wait(
                STATS_SESSION_INFO,
                "",
                bot_id="b1",
                system_notification="你刚刚在旁观群聊，现在决定主动插话。",
                require_buffer=True,
            )
        )
        self.assertEqual(out, [])
        self.assertEqual(await self.store.count("c1"), 0, "主动轮放弃时不应有任何消息落库")

    async def test_notification_only_still_runs_without_flag(self):
        """不设 require_buffer（定时任务唤醒语义）：通知单独可运行的行为保持不变"""
        session = self._make_session([resp_text("定时任务产出")])
        out = await self._collect(
            session.send_and_wait(
                STATS_SESSION_INFO,
                "",
                bot_id="b1",
                system_notification="定时任务：提醒喝水",
            )
        )
        self.assertEqual(out, ["定时任务产出"])

    async def test_require_buffer_with_content_runs(self):
        """buffer 有内容时 require_buffer 不影响正常接话"""
        session = self._make_session([resp_text("主动接话")])
        await session.add_message("[20:51] 区凶冈区：这段好难")
        out = await self._collect(
            session.send_and_wait(
                STATS_SESSION_INFO,
                "",
                bot_id="b1",
                system_notification="你刚刚在旁观群聊，现在决定主动插话。",
                require_buffer=True,
            )
        )
        self.assertEqual(out, ["主动接话"])
        self.assertEqual(await self.store.count("c1"), 2, "缓冲消息与回复均落库")

    async def test_race_passive_consumes_buffer_proactive_aborts(self):
        """端到端竞态：主动轮阻塞在会话锁上时被动轮消费并回复了同一批消息 → 主动轮放弃"""
        session = self._make_session([resp_text("被动回复")])
        await session.add_message("[20:51] 区凶冈区：这段好难")

        # 被动轮推进到首个产出：生成器悬停在 yield 上，仍持有会话锁、buffer 未清
        passive = session.send_and_wait(STATS_SESSION_INFO, "妈妈帮我看看", bot_id="b1")
        it = passive.__aiter__()
        first = await it.__anext__()
        self.assertEqual(first, "被动回复")
        self.assertTrue(session.lock.locked(), "被动轮持有会话锁")
        self.assertTrue(session.buffer, "前置检查视角：buffer 仍非空（竞态窗口）")

        # 主动回复轮：外层检查时 buffer 非空，随后阻塞在会话锁上
        proactive = asyncio.create_task(
            self._collect(
                session.send_and_wait(
                    STATS_SESSION_INFO,
                    "",
                    bot_id="b1",
                    system_notification="你刚刚在旁观群聊，现在决定主动插话。",
                    require_buffer=True,
                )
            )
        )
        await asyncio.sleep(0.05)
        self.assertFalse(proactive.done(), "主动轮应阻塞在会话锁上")

        # 被动轮收尾：finally 清空 buffer、释放锁 → 主动轮取得锁后放弃接话
        rest = [content async for content in it]
        self.assertEqual(rest, [])
        self.assertEqual(await proactive, [], "同一批消息已被被动轮回复，主动轮应放弃")
        # 只有被动轮的消息落库（缓冲消息 + 回复）
        self.assertEqual(await self.store.count("c1"), 2)

    async def test_executor_wires_require_buffer(self):
        """执行器接线：send_auto_reply 必须以 require_buffer=True 调用（防重判不失效）"""
        recorded: dict = {}

        def fake_send_and_wait(session_info, prompt, bot_id, **kwargs):
            recorded.update(kwargs, prompt=prompt, bot_id=bot_id)

            async def gen():
                yield "主动接话"

            return gen()

        sent: list[str] = []

        async def fake_send_proactive(bot, session_info, content):
            sent.append(content)

        on_content_records: list[str] = []

        async def on_content(content):
            on_content_records.append(content)

        class FakeBot:
            self_id = "b1"

        fake_session = SimpleNamespace(
            buffer=["[20:51] 区凶冈区：这段好难"],
            send_and_wait=fake_send_and_wait,
        )
        with (
            mock.patch.object(
                proactive_executor,
                "chat_manager",
                SimpleNamespace(get_session=lambda sid: fake_session),
            ),
            mock.patch.object(proactive_executor, "send_proactive", fake_send_proactive),
        ):
            await proactive_executor.send_auto_reply(
                FakeBot(), STATS_SESSION_INFO, reason="有自然接话点", on_content=on_content
            )
        self.assertTrue(recorded.get("require_buffer"), "主动回复必须传 require_buffer=True")
        self.assertEqual(recorded.get("prompt"), "")
        self.assertIn("有自然接话点", recorded.get("system_notification", ""))
        self.assertEqual(sent, ["主动接话"])
        self.assertEqual(on_content_records, ["主动接话"])


# ===== 决策历史每日清空（2026-10-27） =====


class GateDailyResetTest(unittest.IsolatedAsyncioTestCase):
    """每日清空：内存上下文归零、DB 全量保留、重启仅恢复当日（以每日清空时刻为界）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = compaction_mod.GateStore(
            SessionStore(Path(self.tmp.name) / "sessions").registry
        )

    def _set_created_at(self, session_id: str, ts: float, *, row_id: int | None = None) -> None:
        """直接改写行时间戳，模拟跨日写入"""
        db = self.store.registry.get(session_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path)) as conn:
            if row_id is None:
                conn.execute("UPDATE gate_messages SET created_at = ?", (ts,))
            else:
                conn.execute("UPDATE gate_messages SET created_at = ? WHERE id = ?", (ts, row_id))
            conn.commit()

    def test_daily_reset_boundary(self):
        """清空边界：默认 04:00，之前算昨日；恰在边界属当天；时刻随配置"""
        tz = ZoneInfo("Asia/Shanghai")
        self.assertEqual(
            daily_reset_boundary(datetime(2026, 10, 27, 10, 0, tzinfo=tz)),
            datetime(2026, 10, 27, 4, 0, tzinfo=tz),
        )
        self.assertEqual(
            daily_reset_boundary(datetime(2026, 10, 27, 3, 59, 59, tzinfo=tz)),
            datetime(2026, 10, 26, 4, 0, tzinfo=tz),
        )
        self.assertEqual(
            daily_reset_boundary(datetime(2026, 10, 27, 4, 0, tzinfo=tz)),
            datetime(2026, 10, 27, 4, 0, tzinfo=tz),
        )
        # 时刻可配置：改 00:00 后以午夜为界
        from kanade_bot.plugins.chat.config import cfg as chat_cfg

        saved = chat_cfg.auto_reply.daily_reset_time
        chat_cfg.auto_reply.daily_reset_time = time(0, 0)
        try:
            self.assertEqual(
                daily_reset_boundary(datetime(2026, 10, 27, 2, 30, tzinfo=tz)),
                datetime(2026, 10, 27, 0, 0, tzinfo=tz),
            )
        finally:
            chat_cfg.auto_reply.daily_reset_time = saved

    async def test_restore_today_filters_old_rows(self):
        """当日恢复：边界前的行不进上下文；DB 全量保留"""
        boundary = daily_reset_boundary().timestamp()
        old_ids = await self.store.append_gate_messages(
            "s1",
            [("user", "昨日消息")],
        )
        for row_id in old_ids:
            self._set_created_at("s1", boundary - 3600, row_id=row_id)
        await self.store.append_gate_messages(
            "s1", [("user", "今日消息"), ("assistant", '{"reply": false}')]
        )

        # 全量口径（since=None）：全部普通行加载
        snap_all = await self.store.load_gate_history("s1")
        self.assertEqual(len(snap_all.rows), 3)
        # 当日口径：昨日行被过滤
        snap_today = await self.store.load_gate_history("s1", since=boundary)
        self.assertEqual([row[2] for row in snap_today.rows], ["今日消息", '{"reply": false}'])
        self.assertEqual(snap_today.total_rows, 3, "DB 全量行保留不删")

        history = SessionGateHistory(self.store, "s1")
        await history.restore_today()
        self.assertEqual([row.content for row in history.rows], ["今日消息", '{"reply": false}'])

    async def test_reset_context_memory_only_db_kept(self):
        """每日清空：内存生效序列归零；DB 行保留，全量口径仍完整可恢复"""
        history = SessionGateHistory(self.store, "s1")
        await history.append("user", "消息1")
        await history.append("assistant", '{"reply": false}')
        await history.append("user", "消息2")
        self.assertEqual(len(history.rows), 3)

        history.reset_context()
        self.assertEqual(history.rows, [])

        # 行属昨日时：当日恢复为空（相当于错过的每日清空在恢复时补齐），全量恢复完整
        self._set_created_at("s1", daily_reset_boundary().timestamp() - 3600)
        await history.restore_today()
        self.assertEqual(history.rows, [], "昨日行不进当日上下文")
        await history.restore()
        self.assertEqual(len(history.rows), 3, "全量口径：DB 行完整保留")


class WorkspaceTreeTest(unittest.TestCase):
    """`list_workspace_files`：tree 风格渲染 + 限深限量"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_tree_rendering(self):
        """目录/文件按名混排，带树形连接符与缩进"""
        (self.root / "assets").mkdir()
        (self.root / "assets" / "b.jpg").write_bytes(b"x" * 2048)
        (self.root / "assets" / "a.jpg").write_bytes(b"x")
        (self.root / "main.py").write_text("print()")

        lines = session_mod.list_workspace_files(self.root)
        self.assertEqual(
            lines,
            [
                "├ assets/",
                "│ ├ a.jpg (1 B)",
                "│ └ b.jpg (2.0 KB)",
                "└ main.py (7 B)",
            ],
        )

    def test_generated_dirs_excluded(self):
        """顶层 images/ 与 rendered/ 为工具生成内容，整目录不展示；嵌套同名目录不受影响"""
        (self.root / "images").mkdir()
        (self.root / "images" / "a.jpg").write_bytes(b"x")
        (self.root / "rendered").mkdir()
        (self.root / "rendered" / "out.png").write_bytes(b"x")
        (self.root / "nested").mkdir()
        (self.root / "nested" / "images").mkdir()
        (self.root / "nested" / "images" / "keep.txt").write_text("x")

        lines = session_mod.list_workspace_files(self.root)
        self.assertEqual(
            lines,
            ["└ nested/", "  └ images/", "    └ keep.txt (1 B)"],
        )

    def test_hidden_entries_excluded(self):
        """`.` 开头的隐藏文件/目录默认不展示（含嵌套层级）"""
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "bin").mkdir()
        (self.root / ".bashrc").write_text("x")
        (self.root / "sub").mkdir()
        (self.root / "sub" / ".hidden").mkdir()
        (self.root / "sub" / ".secret").write_text("x")
        (self.root / "sub" / "visible.txt").write_text("x")

        lines = session_mod.list_workspace_files(self.root)
        self.assertEqual(lines, ["└ sub/", "  └ visible.txt (1 B)"])

    def test_entry_limit_keeps_marker(self):
        """超出条数上限时截断并追加省略标记"""
        for i in range(15):
            (self.root / f"f{i:02d}.txt").write_text("x")

        lines = session_mod.list_workspace_files(self.root)
        self.assertEqual(len(lines), session_mod.MAX_WORKSPACE_ENTRIES + 1)
        self.assertEqual(lines[-1], "…")

    def test_depth_limit_lists_but_not_descends(self):
        """第 3 层目录仍列出，但不再下钻展示其内容"""
        deep = self.root / "d1" / "d2" / "d3"
        deep.mkdir(parents=True)
        (deep / "hidden.txt").write_text("x")

        lines = session_mod.list_workspace_files(self.root)
        # d1/d2 均为独子：延续前缀全为空格，无 │ 竖线
        self.assertIn("    └ d3/", lines)
        self.assertNotIn("hidden.txt", "\n".join(lines))

    def test_empty_or_missing_root(self):
        """根目录不存在/为空时返回空列表"""
        self.assertEqual(session_mod.list_workspace_files(self.root), [])
        self.assertEqual(session_mod.list_workspace_files(self.root / "missing"), [])


if __name__ == "__main__":
    unittest.main()
