from __future__ import annotations

import time
from collections.abc import Callable
from copy import copy
from dataclasses import dataclass, field
from math import ceil
from typing import TYPE_CHECKING, Any, Literal, cast

from nonebot import logger
from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
    compact_now,
    estimate_context_tokens,
)

from kanade_bot.utils.session import SessionInfo

from ..config import CompactionConfig
from .message_blobs import blob_marker_messages

if TYPE_CHECKING:
    # 仅类型标注使用；运行时导入会造成 compaction 和 session_store 循环导入
    from .session_store import SessionStore

type CompactionStrategy = Literal["clear_tool_results", "summarizing"]

MIN_OUTPUT_TOKENS = 4096
"""保留给正常回答的最小输出预算"""

MIN_SAFETY_MARGIN_TOKENS = 8192
"""输入估算之外的最小安全余量"""

IMAGE_TOKEN_ESTIMATE = 1024
"""DeepSeek视觉模型单张图片的保守token上限"""


class ContextBudgetError(RuntimeError):
    """请求在本地预算检查阶段已无法安全发送"""


def context_safety_margin(context_window: int) -> int:
    """为估算误差和工具 schema 预留的安全余量"""

    return max(MIN_SAFETY_MARGIN_TOKENS, ceil(context_window * 0.05))


def _image_content_count(messages: list[ModelMessage], start: int = 0) -> int:
    """统计 usage anchor 之后的图片数量

    pydantic-ai的通用估算不把二进制 payload 当文本计数。这里不按文件字节数换算：
    DeepSeek单张图片最多占用约 1024 tokens，原始JPEG/PNG大小与计费token数没有线性关系。
    """
    total = 0
    for message in messages[start:]:
        for part in getattr(message, "parts", ()):
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content if not isinstance(part.content, str) else [part.content]
            total += sum(1 for item in content if isinstance(item, BinaryContent) and item.is_image)
    return total


def estimate_request_tokens(
    messages: list[ModelMessage],
    request_parameters: ModelRequestParameters | None = None,
    *,
    image_tokens_per_image: int = IMAGE_TOKEN_ESTIMATE,
) -> int:
    """估算即将发送的输入 tokens

    增强Pydantic AI Harness的通用估算。

    provider usage anchor 已覆盖其之前的图片；anchor 之后新增的图片按DeepSeek
    的单张上限计入，避免本轮图片绕过压缩阈值，也避免按文件大小严重高估。
    """

    estimated = estimate_context_tokens(messages, model_request_parameters=request_parameters)
    anchor = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelResponse) and message.usage.input_tokens > 0:
            anchor = index
            break
    return estimated + _image_content_count(messages, anchor + 1) * image_tokens_per_image


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


@dataclass
class RequestBudgetCapability(AbstractCapability[Any]):
    """在压缩后的请求上重新计算输出预算，并阻止必然越界的请求。"""

    context_window: int | None
    output_upper_tokens: int | None
    image_tokens_per_image: int = IMAGE_TOKEN_ESTIMATE

    def get_model_settings(self):
        def resolve(ctx: RunContext[Any]) -> ModelSettings:
            settings = copy(ctx.model_settings) or ModelSettings()
            configured = self.output_upper_tokens or settings.get("max_tokens")
            if self.context_window is None:
                return {"max_tokens": configured} if configured else {}
            estimated = estimate_request_tokens(
                list(ctx.messages), image_tokens_per_image=self.image_tokens_per_image
            )
            available = self.context_window - estimated - context_safety_margin(self.context_window)
            requested = configured if isinstance(configured, int) else available
            return {"max_tokens": max(0, min(requested, available))}

        return resolve

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        window = self.context_window or request_context.model.context_window
        if window is None:
            return request_context

        estimated = estimate_request_tokens(
            request_context.messages,
            request_context.model_request_parameters,
            image_tokens_per_image=self.image_tokens_per_image,
        )
        available = window - estimated - context_safety_margin(window)
        if available < MIN_OUTPUT_TOKENS:
            raise ContextBudgetError(
                f"上下文预算不足：估算输入 {estimated} tokens，窗口 {window}，"
                f"安全余量 {context_safety_margin(window)}，无法保留至少 {MIN_OUTPUT_TOKENS} tokens 输出"
            )

        # 拷贝后修改
        settings = ModelSettings(request_context.model_settings or {})
        configured = self.output_upper_tokens or settings.get("max_tokens")
        effective = min(configured or available, available)
        if effective < MIN_OUTPUT_TOKENS:
            raise ContextBudgetError(
                f"上下文预算不足：可用输出 {effective} tokens，低于最小预算 {MIN_OUTPUT_TOKENS}"
            )
        settings["max_tokens"] = effective
        request_context.model_settings = settings
        return request_context


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


def build_summary(
    params: CompactionConfig,
    *,
    model_settings: ModelSettings | None = None,
    output_upper_tokens: int | None = None,
) -> SummarizingCompaction:
    """构造摘要档策略

    轮末自动压缩、在线应急阀与手动压缩共用同一构造

    手动压缩经 `compact_now` 直接调用 `compact`，不经过触发判断，
    `max_fraction` 仅为满足构造校验；摘要档未启用时以 1.0 占位。

    `preserve_first_user_message=False`：长期群聊里第一条用户消息无意义。
    """
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
    """消息列表指纹

    在 blob 标记形态上序列化：图片等二进制内容替换为内容 hash 标记，
    指纹对二进制内容仍敏感（hash 由内容决定），但不再序列化 base64 巨串。
    """
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
    """轮末自动压缩：最终回复落库后、下一轮开始前执行

    常规压缩的唯一路径。与在线应急阀`build_compaction_capability`共用同一套分层策略，
    但闸门用常规阈值：摘要档启用时为 `summary_target_fraction`
    （clear→summary依次升级），未启用时退化为 `trigger_fraction` 上的零成本档。

    `messages` 传调用方 run 结束时的完整历史，避免重复 restore；
    在稳定存储状态上判定与压缩，落 mark 后下一轮 `restore` 重放即得同样结果。
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
    if compacted is messages or _messages_fingerprint(compacted) == _messages_fingerprint(messages):
        return None

    # mark 类型自验证（与 persist_run 同一判定）：零成本档对输入重放能
    # 复现产物 → 只存参数（可重放）；否则视为摘要产物落快照
    mark: CompactionMark = build_summary_mark(params, compacted)
    if _messages_fingerprint(await apply_strategy(messages, build_clear_mark(params))) == (
        _messages_fingerprint(compacted)
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
    """永不改写请求上下文的占位 capability

    `online_valve_fraction=None`（完全关闭在线压缩）时充当应急阀的空策略，
    使 `RecordingCompaction` / `persist_run` 的记录路径保持统一。
    """

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
    """构造 run 内应急压缩 capability（非常规压缩路径）

    常规压缩在轮末进行（`compact_between_turns`）；
    本 capability 仅在估算超过 `online_valve_fraction` 时才在 run 内压缩，
    防止单轮内上下文暴涨击穿窗口导致 provider 报错。
    """
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
