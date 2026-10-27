"""会话压缩：分层策略构造、压缩事件 mark 与轮末压缩落库"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

from nonebot import logger
from pydantic_ai import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelRequestContext,
    ModelSettings,
    RunContext,
    SystemPromptPart,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import Model
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness import ClearToolResults, SummarizingCompaction, TieredCompaction
from pydantic_ai_harness.compaction import compact_now, estimate_context_tokens

from kanade_bot.utils.session import SessionInfo

from ...config import CompactionConfig
from .blobs import blob_marker_messages
from .budget import (
    MIN_OUTPUT_TOKENS,
    ContextBudgetError,
    context_safety_margin,
    estimate_request_tokens,
)

if TYPE_CHECKING:
    # 仅类型标注使用；运行时导入会造成 compaction 和 store 循环导入
    from .store import SessionStore

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
    """本条 mark 覆盖到 DB 的消息 seq（含）；-1 表示覆盖全部"""


def build_clear(params: CompactionConfig, *, online: bool) -> ClearToolResults:
    """构造零成本档策略（恢复重放时忽略 `min_clear_tokens`）"""

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


def _summary_prompt_template(params: CompactionConfig) -> str | None:
    """读入摘要档提示词模板文件；缺失或非法时返回 None（不覆盖，用 harness 默认）"""
    path = params.summary_prompt_file_path
    if not path.is_file():
        logger.warning(f"摘要档提示词文件不存在，使用 harness 默认模板：{path.absolute()}")
        return None
    template = path.read_text(encoding="utf-8")
    try:
        if not template.strip() or "{messages}" not in template:
            raise ValueError("模板为空或缺少 {messages} 占位符")
        template.format(messages="")  # 干跑校验：无其它未转义花括号
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(f"摘要档提示词模板非法（{e}），已回退 harness 默认：{path}")
        return None
    return template


def summary_model_settings(
    params: CompactionConfig,
    base: ModelSettings | None,
    output_upper_tokens: int | None,
) -> ModelSettings | Callable[[RunContext[Any]], ModelSettings] | None:
    """为摘要请求继承主模型设置，并为摘要输出保留上下文空间。"""
    settings = ModelSettings(base or {})
    window = params.context_window
    if window is None or (base is None and output_upper_tokens is None):
        return settings or None

    def resolve(ctx: RunContext[Any]) -> ModelSettings:
        resolved = ModelSettings(settings)
        estimated = estimate_request_tokens(list(ctx.messages))
        available = window - estimated - context_safety_margin(window)
        if available < MIN_OUTPUT_TOKENS:
            raise ContextBudgetError(
                f"摘要上下文预算不足：估算输入 {estimated} tokens，窗口 {window}，"
                f"无法保留至少 {MIN_OUTPUT_TOKENS} tokens 输出"
            )
        resolved["max_tokens"] = min(output_upper_tokens or available, available)
        return resolved

    return resolve


def build_summary(
    params: CompactionConfig,
    *,
    model_settings: ModelSettings | None = None,
    output_upper_tokens: int | None = None,
) -> SummarizingCompaction:
    """构造摘要档策略（轮末、应急阀与手动压缩共用）"""
    # 模板文件缺失/非法时不覆盖提示词字段，回退 harness 默认
    prompt_kwargs: dict[str, Any] = {}
    if (template := _summary_prompt_template(params)) is not None:
        prompt_kwargs = {"summary_prompt": template}
    return SummarizingCompaction(
        model=params.summary_model,
        max_fraction=params.summary_target_fraction or 1.0,
        keep_messages=params.summary_keep_messages,
        keep_user_messages=params.summary_keep_user_messages,
        preserve_first_user_message=False,
        # 上游桩类型未声明 callable 形态，运行时经嵌套 Agent(model_settings=) 生效
        model_settings=cast(
            ModelSettings | None,
            summary_model_settings(params, model_settings, output_upper_tokens),
        ),
        **prompt_kwargs,
    )


def build_summary_mark(params: CompactionConfig, result: list[ModelMessage]) -> CompactionMark:
    """按当前参数与摘要产物构造一条摘要档 mark（`up_to_seq` 由调用方填写）"""
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
    """把一条压缩事件应用到消息列表上（参数一律取自 `mark.params`）"""
    params = CompactionConfig.model_validate(mark.params)

    if mark.result is not None:
        # 摘要档：LLM 非确定性，直接用存下来的产物，不重新生成
        return ModelMessagesTypeAdapter.validate_json(mark.result)

    # 零成本档：确定性 + 幂等的纯函数，用记录参数重跑
    strategy = build_clear(params, online=False)
    return await compact_now(strategy, messages, model=model if model is not None else TestModel())


def messages_fingerprint(messages: list[ModelMessage]) -> bytes:
    """消息列表指纹（blob 标记形态序列化，见 SESSION_STORE_DESIGN.md §5）"""
    return ModelMessagesTypeAdapter.dump_json(blob_marker_messages(messages))


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
    """包住分层压缩策略，按会话记录 run 内发生的每次压缩"""

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
        before = list(ctx.messages)
        result = await self.strategy.before_model_request(ctx, request_context)
        if session_id is not None and messages_fingerprint(before) != messages_fingerprint(
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
        if messages_fingerprint([a[i]]) != messages_fingerprint([b[i]]):
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

    base: 本 run 开始时的历史；all_messages: run 结束时的完整历史；
    new_messages: `stream.new_messages()`（仅无压缩时可靠）。
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

    # 2. mark 区间覆盖 post 及其后至 run 结束；尾部被 repair 改写时以改写后形态为准
    last_post = events[-1].post
    c_tail = _common_prefix_len(last_post, all_messages)
    rewritten = rewritten or c_tail < len(last_post)
    snapshot = all_messages[:c_tail]

    mark = build_summary_mark(capability.params, snapshot)
    if not rewritten and len(events) == 1:
        # 自验证：零成本档重放能复现快照则只存参数（零额外存储）
        candidate = build_clear_mark(capability.params)
        if messages_fingerprint(await apply_strategy(events[0].pre, candidate)) == (
            messages_fingerprint(snapshot)
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


async def compact_between_turns(
    store: SessionStore,
    session_id: str,
    params: CompactionConfig,
    messages: list[ModelMessage],
    *,
    model: Model | str,
    model_settings: ModelSettings | None = None,
    output_upper_tokens: int | None = None,
) -> dict[str, Any] | None:
    """轮末自动压缩：最终回复落库后、下一轮开始前执行（常规压缩唯一路径）

    未触发 / 窗口未固化返回 None。
    """
    if params.context_window is None or not messages:
        # 窗口未固化时比例闸门无从计算；应急阀仍会自行回退解析，此处保守跳过
        return None

    if params.summary_target_fraction:
        gate = params.summary_target_fraction
        strategy: AbstractCapability[Any] = TieredCompaction(
            tiers=[
                build_clear(params, online=True),
                build_summary(
                    params,
                    model_settings=model_settings,
                    output_upper_tokens=output_upper_tokens,
                ),
            ],
            target_fraction=gate,
            context_window=params.context_window,
        )
    else:
        gate = params.trigger_fraction
        strategy = build_clear(params, online=True)

    est = estimate_context_tokens(messages)
    if est <= gate * params.context_window:
        return None

    compacted = await compact_now(strategy, messages, model=model, conversation_id=session_id)
    if compacted is messages or messages_fingerprint(compacted) == messages_fingerprint(messages):
        return None

    # mark 类型自验证（与 persist_run 同一判定）
    mark: CompactionMark = build_summary_mark(params, compacted)
    if messages_fingerprint(await apply_strategy(messages, build_clear_mark(params))) == (
        messages_fingerprint(compacted)
    ):
        mark = build_clear_mark(params)
    total = await store.count(session_id)
    mark.up_to_seq = total - 1
    await store.add_compaction_mark(session_id, mark)
    logger.debug(
        f"会话{session_id}轮末压缩：策略={mark.strategy}，"
        f"{len(messages)}条→{len(compacted)}条，估算{est}→{estimate_context_tokens(compacted)}tokens"
    )
    return {
        "strategy": mark.strategy,
        "before": len(messages),
        "after": len(compacted),
        "tokens_before": est,
        "tokens_after": estimate_context_tokens(compacted),
    }


class _NoopCapability(AbstractCapability[Any]):
    """在线压缩完全关闭时的空策略，使记录路径保持统一"""

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        return request_context


def build_compaction_capability(
    params: CompactionConfig = CompactionConfig(),
    *,
    output_upper_tokens: int | None = None,
    model_settings: ModelSettings | None = None,
):
    """构造 run 内应急压缩 capability（仅防单轮内上下文暴涨击穿窗口）"""
    if params.online_valve_fraction is None:
        strategy: AbstractCapability[Any] = _NoopCapability()
        return RecordingCompaction(strategy=strategy, params=params)

    clear = build_clear(params, online=True)
    if not params.summary_target_fraction:
        strategy = TieredCompaction(
            tiers=[clear],
            target_fraction=params.online_valve_fraction,
            context_window=params.context_window,
        )
    else:
        strategy = TieredCompaction(
            tiers=[
                clear,
                build_summary(
                    params,
                    model_settings=model_settings,
                    output_upper_tokens=output_upper_tokens,
                ),
            ],
            target_fraction=params.online_valve_fraction,
            context_window=params.context_window,
        )
    return RecordingCompaction(strategy=strategy, params=params)
