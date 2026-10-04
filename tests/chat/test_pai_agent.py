"""聊天 Agent 端到端验证（FunctionModel 驱动，不打真实模型）。

覆盖聊天 Agent 的核心行为：
- 压缩的确定性 / 幂等 / 配对保持（压缩落库方案的前提）
- `SessionStore` append-only 存储 + 压缩事件落库
- **恢复一致性**（压缩事件可重放的前提）
- `run_stream_events` 的事件分流与中断

`compaction.py` 与 `session_store.py` 均位于 `kanade_bot/plugins/chat/agent/`。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

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

from pydantic_ai import Agent
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.messages import (
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
from pydantic_ai.usage import UsageLimits

from kanade_bot.plugins.chat.agent import session_store as store_mod
from kanade_bot.plugins.chat.agent import compaction as compaction_mod
from kanade_bot.plugins.chat.config import CompactionConfig as CompactionParams

SessionStore = store_mod.SessionStore

CLEARED = "[tool result cleared]"

TEST_PARAMS = CompactionParams(
    trigger_fraction=0.5,
    # context_window 压到很小，让测试历史必然触发压缩；
    # 生产用 trigger_fraction 相对模型真实窗口，不设这一项。
    context_window=200,
    keep_pairs=1,
    # 清理收益至少 1 个 token 才动手，相当于「不设阈值」
    min_clear_tokens=1,
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
        self.store = SessionStore(Path(self.tmp.name) / "sessions.sqlite3")

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
        await self.store.add_compaction_mark("c1", compaction_mod.build_clear_mark(TEST_PARAMS))

        restored = await self.store.restore("c1", params=TEST_PARAMS)
        self.assertEqual(len(restored), len(history), "消息条数应保持不变")
        self.assertLess(_uncleared(restored), _uncleared(history), "旧工具结果应已清空")
        self.assertEqual(await self.store.count("c1"), len(history), "DB 保留全量")
        self.assertEqual(fp(await self.store.load("c1")), fp(history))

    async def test_truncate_removes_tail_and_marks(self):
        await self.store.append("c1", make_history(4))
        await self.store.add_compaction_mark("c1", compaction_mod.build_clear_mark(TEST_PARAMS))
        await self.store.truncate("c1", 6)
        self.assertEqual(await self.store.count("c1"), 6)
        self.assertEqual(len(await self.store.load_compaction_marks("c1")), 0)

    async def test_clear_removes_everything(self):
        await self.store.append("c1", make_history(2))
        await self.store.add_compaction_mark("c1", compaction_mod.build_clear_mark(TEST_PARAMS))
        await self.store.clear("c1")
        self.assertEqual(await self.store.count("c1"), 0)
        self.assertEqual(await self.store.load("c1"), [])


# ===== 恢复一致性（压缩事件可重放的前提） =====


class RestoreConsistencyTest(unittest.IsolatedAsyncioTestCase):
    """恢复的历史必须与关闭前完全一致"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "sessions.sqlite3"

    async def test_restored_matches_online_history(self):
        """记录「关闭前实际发送的消息指纹」→ 重启 → 重建 → 指纹必须相同"""
        store = SessionStore(self.db)

        # ---- 关闭前：跑一轮（工具调用 + 收尾），期间触发压缩 ----
        history = make_history(4)
        await store.append("c1", history)

        recorder = compaction_mod.build_compaction_capability(TEST_PARAMS)
        agent, scripted = build_agent([resp_tool("t", {"i": 1}, "n1"), resp_text("done")], recorder)

        result = await agent.run("问题A", message_history=history)
        await store.append("c1", result.new_messages())

        final_history = repair_messages(result.all_messages())
        mark = await recorder.take_mark(final_history)
        self.assertIsNotNone(mark, "本轮应发生压缩")
        await store.add_compaction_mark("c1", mark)

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
        _, next_scripted = build_agent([resp_text("下一轮")])
        next_agent, _ = build_agent([resp_text("下一轮")])
        del next_scripted
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
        await store.add_compaction_mark(
            "c1", compaction_mod.build_clear_mark(CompactionParams(keep_pairs=1))
        )

        new_params = CompactionParams(keep_pairs=3)
        reborn = SessionStore(self.db)
        restored = await reborn.restore("c1", params=new_params)

        expected = await compaction_mod.apply_strategy(
            history, compaction_mod.build_clear_mark(new_params)
        )
        self.assertEqual(fp(restored), fp(expected))
        self.assertEqual(
            len(await reborn.load_compaction_marks("c1")), 0, "漂移后旧 marks 应被丢弃"
        )

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
                result=compaction_mod._messages_fingerprint(snapshot),
                fingerprint=TEST_PARAMS.fingerprint(),
                applied_at=0.0,
            ),
        )

        reborn = SessionStore(self.db)
        restored = await reborn.restore("c1", params=TEST_PARAMS)
        self.assertEqual(fp(restored), fp(snapshot))


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


if __name__ == "__main__":
    unittest.main()
