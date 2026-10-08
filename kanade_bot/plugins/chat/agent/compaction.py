from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from nonebot import logger
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    SystemPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
    compact_now,
)

from kanade_bot.utils.session import SessionInfo

from ..config import CompactionConfig

if TYPE_CHECKING:
    # 仅类型标注使用；运行时导入会造成 compaction ⇄ session_store 循环导入
    from .session_store import SessionStore

type CompactionStrategy = Literal["clear_tool_results", "summarizing"]


@dataclass
class CompactionMark:
    """压缩事件

    记录历史已按 (strategy, params) 压缩过"""

    strategy: CompactionStrategy
    """压缩策略"""

    params: dict[str, Any] = field(default_factory=dict)
    """压缩参数"""

    result: bytes | None = None
    """不可重放策略的产物，可重放策略为 None"""

    fingerprint: str = ""
    """参数指纹，用于检测配置漂移"""

    applied_at: float = 0.0
    """记录时间戳"""

    up_to_seq: int = -1
    """本条 mark 覆盖到 DB 的消息 seq（含）

    恢复时只把 mark 应用到该 seq 为止的前缀上，再拼回其后追加的消息；
    `-1` 表示覆盖全部（旧数据兼容，等价于整体替换）。
    """


def build_clear(params: CompactionConfig, *, online: bool) -> ClearToolResults:
    """构造零成本档策略

    `online=False`（恢复重放）时**忽略 `min_clear_tokens`**：
    该阈值依赖 provider上报的 usage 锚点估计 token，重放时没有同样的锚点。
    mark 记录的是「确实清理过」这个事实，所以重放无条件执行。
    """

    return ClearToolResults(
        max_fraction=params.trigger_fraction,
        context_window=params.context_window,
        keep_pairs=params.keep_pairs,
        min_clear_tokens=params.min_clear_tokens if online else None,
    )


def build_clear_mark(params: CompactionConfig) -> CompactionMark:
    """按当前参数构造一条零成本档 mark（参数漂移后从全量重放用）"""
    return CompactionMark(
        strategy="clear_tool_results",
        params=params.model_dump(),
        result=None,
        fingerprint=params.fingerprint(),
        applied_at=time.time(),
    )


def build_summary(params: CompactionConfig) -> SummarizingCompaction:
    """构造摘要档策略（在线分层压缩与手动压缩共用同一构造）

    手动压缩经 `compact_now` 直接调用 `compact`，不经过触发判断，
    `max_fraction` 仅为满足构造校验；摘要档未启用
    （`summary_target_fraction` 为 None）时以 1.0 占位。
    """
    return SummarizingCompaction(
        model=params.summary_model,
        max_fraction=params.summary_target_fraction or 1.0,
        keep_messages=params.summary_keep_messages,
    )


def build_summary_mark(params: CompactionConfig, result: list[ModelMessage]) -> CompactionMark:
    """按当前参数与摘要产物构造一条摘要档 mark

    摘要由 LLM 生成、非确定性，无法重放，必须把完整产物存进
    `result`，恢复时直接使用（见 `apply_strategy`）。
    `up_to_seq` 由调用方（`persist_run` / 手动压缩）按落库边界填写。
    """
    return CompactionMark(
        strategy="summarizing",
        params=params.model_dump(),
        result=ModelMessagesTypeAdapter.dump_json(result),
        fingerprint=params.fingerprint(),
        applied_at=time.time(),
    )


_SUMMARY_PREFIX = "Summary of previous conversation:\n\n"
"""摘要 Part 的前缀（与 pydantic_ai_harness `_summarizing_compaction` 约定一致）"""


def extract_summary(messages: list[ModelMessage]) -> str | None:
    """从压缩后的历史中取出摘要文本（手动压缩命令预览用），无摘要返回 None"""
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            continue
        for part in msg.parts:
            if isinstance(part, SystemPromptPart):
                text = part.content.removeprefix(_SUMMARY_PREFIX)
                return text.strip() or None
    return None


async def apply_strategy(
    messages: list[ModelMessage],
    mark: CompactionMark,
    *,
    model: Model | str | None = None,
) -> list[ModelMessage]:
    """把一条压缩事件应用到消息列表上

    在线压缩与恢复重放都必须经过本函数，参数一律取自 `mark.params`
    """
    params = CompactionConfig.model_validate(mark.params)

    if mark.result is not None:
        # 摘要档：LLM 非确定性，直接用存下来的产物，不重新生成
        return ModelMessagesTypeAdapter.validate_json(mark.result)

    # 零成本档：确定性 + 幂等的纯函数，用记录参数重跑
    strategy = build_clear(params, online=False)
    return await compact_now(strategy, messages, model=model if model is not None else TestModel())


def _messages_fingerprint(messages: list[ModelMessage]) -> bytes:
    """消息列表指纹"""
    return ModelMessagesTypeAdapter.dump_json(messages)


def _session_id_of(ctx: RunContext[Any]) -> str | None:
    """从 RunContext.deps 尽力取会话 ID（无法识别时返回 None）"""
    session_info: SessionInfo | None = getattr(ctx.deps, "session_info", None)
    return session_info.session_id if session_info else None


@dataclass
class CompactionEvent:
    """run 内一次在线压缩的前后快照"""

    pre: list[ModelMessage]
    """压缩发生前的完整历史（含本轮已产生的消息）"""

    post: list[ModelMessage]
    """压缩后的完整历史"""


@dataclass
class RecordingCompaction[AgentDepsT](AbstractCapability[AgentDepsT]):
    """包住分层压缩策略，按会话记录 run 内发生的每次压缩

    压缩只改写 run 内部的消息列表，调用方（`persist_run`）必须在 run
    结束后取出事件，把压缩前尚未落库的消息补写入库、按落库边界记录
    mark，再持久化压缩后新产生的消息。
    """

    strategy: AbstractCapability[AgentDepsT]
    """在线压缩策略"""

    params: CompactionConfig
    """当前压缩参数"""

    events: dict[str, list[CompactionEvent]] = field(default_factory=dict)
    """会话 → 本 run 内的压缩事件"""

    async def before_model_request(
        self,
        ctx: RunContext[AgentDepsT],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        session_id = _session_id_of(ctx)
        # 必须拷贝快照：压缩策略（TieredCompaction）以 ctx.messages[:] = ... 原地
        # 替换列表，直接引用同一对象会让前后指纹永远相等，压缩事件丢失
        before = list(ctx.messages)
        result = await self.strategy.before_model_request(ctx, request_context)
        if session_id is not None and _messages_fingerprint(before) != _messages_fingerprint(
            list(ctx.messages)
        ):
            self.events.setdefault(session_id, []).append(
                CompactionEvent(pre=before, post=list(ctx.messages))
            )
        return result

    def take_events(self, session_id: str) -> list[CompactionEvent]:
        """取出并清空该会话本 run 内的压缩事件"""
        return self.events.pop(session_id, [])


def _common_prefix_len(a: list[ModelMessage], b: list[ModelMessage]) -> int:
    """两个消息列表的逐字节公共前缀长度"""
    n = min(len(a), len(b))
    for i in range(n):
        if _messages_fingerprint([a[i]]) != _messages_fingerprint([b[i]]):
            return i
    return n


async def persist_run(
    store: SessionStore,
    session_id: str,
    capability: RecordingCompaction[Any],
    base: list[ModelMessage],
    all_messages: list[ModelMessage],
    new_messages: list[ModelMessage],
) -> None:
    """把一次 run 的新增消息与压缩事件落库

    base: 本 run 开始时的历史（与传给 `message_history` 的一致）；
    all_messages: run 结束时的完整历史（`stream.all_messages()`，run 内
    发生过压缩时其前缀与 base 不同——这正是不能用「base + new_messages()」
    重建历史的原因）；
    new_messages: `stream.new_messages()`（pydantic-ai 以 run_id 多层回退
    维护的增量边界，仅在未发生压缩时可靠，此时是官方正确机制）。

    无压缩：等价于把 `new_messages` 追加入库。

    有压缩：对每个事件——补写压缩前未落库的消息（使 DB 行覆盖 `pre`）
    → 记 mark（`up_to_seq` = 补写后的行数-1）→ 把压缩后新产生的消息
    追加为 mark 之后的尾部行。恢复时 `restore` 按 `up_to_seq` 分段
    重放/替换并拼回尾部，重建结果与发送内容一致。

    对齐一律用**内容级公共前缀**而非位置：pydantic-ai 每次请求前跑
    `repair_messages()`（合并相邻请求），可能缩短/改写历史（例如旧库
    分离形态、输入与历史尾部合并），位置切片会错位丢消息。公共前缀
    变短即检测到「改写」——改写后 DB 行无法重放在线输入，mark 强制
    走完整快照（恢复正确性优先，放弃该次的重放优化）。
    """
    events = capability.take_events(session_id)

    if not events:
        if new_messages:
            await store.append(session_id, new_messages)
        return

    # 1. 补写各事件压缩前尚未落库的消息，使 DB 行覆盖到最后一个 pre
    boundary: list[ModelMessage] = base
    rewritten = False
    for event in events:
        c = _common_prefix_len(boundary, event.pre)
        rewritten = rewritten or c < len(boundary)
        if pre_delta := event.pre[c:]:
            await store.append(session_id, pre_delta)
        boundary = event.pre

    # 2. mark 区间的实际内容 = post 及其后至 run 结束；若后续 repair
    #    改写了 post 尾部（公共前缀变短），以改写后形态为准
    last_post = events[-1].post
    c_tail = _common_prefix_len(last_post, all_messages)
    rewritten = rewritten or c_tail < len(last_post)
    snapshot = all_messages[:c_tail]

    mark = build_summary_mark(capability.params, snapshot)
    if not rewritten and len(events) == 1:
        # 自验证：零成本档是确定性纯函数，对 pre 重放能复现快照就只存
        # 参数（零额外存储）
        candidate = build_clear_mark(capability.params)
        if _messages_fingerprint(await apply_strategy(events[0].pre, candidate)) == (
            _messages_fingerprint(snapshot)
        ):
            mark = candidate
    mark.up_to_seq = await store.count(session_id) - 1
    await store.add_compaction_mark(session_id, mark)
    logger.debug(
        f"会话{session_id}记录压缩事件：策略={mark.strategy}，"
        f"up_to_seq={mark.up_to_seq}，可重放={mark.result is None}"
    )

    # 3. 压缩后新产生的消息 → mark 之后的尾部行
    if tail := all_messages[c_tail:]:
        await store.append(session_id, tail)


def build_compaction_capability(params: CompactionConfig = CompactionConfig()):
    """构造压缩 capability"""
    clear = build_clear(params, online=True)
    if not params.summary_target_fraction:
        strategy = clear
    else:
        strategy = TieredCompaction(
            tiers=[clear, build_summary(params)],
            target_fraction=params.summary_target_fraction,
            context_window=params.context_window,
        )
    return RecordingCompaction(strategy=strategy, params=params)
