import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
    compact_now,
)

from ..config import CompactionConfig

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


@dataclass
class RecordingCompaction[AgentDepsT](AbstractCapability[AgentDepsT]):
    """包住分层压缩策略，并记录本轮历史是否被压缩过"""

    strategy: AbstractCapability[AgentDepsT]
    """在线压缩策略"""

    params: CompactionConfig
    """当前压缩参数"""

    compacted: bool = False
    """本轮是否发生过压缩"""

    pre_compaction: list[ModelMessage] | None = None
    """本轮首次压缩前的历史快照"""

    async def before_model_request(
        self,
        ctx: RunContext[AgentDepsT],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        before = list(ctx.messages)
        result = await self.strategy.before_model_request(ctx, request_context)
        if _messages_fingerprint(before) != _messages_fingerprint(list(ctx.messages)):
            self.compacted = True
            if self.pre_compaction is None:
                self.pre_compaction = before
        return result

    async def take_mark(self, final_history: list[ModelMessage]) -> CompactionMark | None:
        """本轮结束后取出一条 mark（无压缩则返回 None），并重置状态"""
        compacted, self.compacted = self.compacted, False
        pre, self.pre_compaction = self.pre_compaction, None
        if not compacted or pre is None:
            return None

        target = _messages_fingerprint(final_history)
        dumped = self.params.model_dump()
        candidate = CompactionMark(
            strategy="clear_tool_results",
            params=dumped,
            result=None,
            fingerprint=self.params.fingerprint(),
            applied_at=time.time(),
        )

        # 自验证：先试零成本档重放，能复现就只存参数（零额外存储）
        if _messages_fingerprint(await apply_strategy(pre, candidate)) == target:
            return candidate
        # 否则说明触发了摘要档，必须存完整快照
        return CompactionMark(
            strategy="summarizing",
            params=dumped,
            result=target,
            fingerprint=self.params.fingerprint(),
            applied_at=time.time(),
        )


def build_compaction_capability(params: CompactionConfig = CompactionConfig()):
    """构造压缩 capability"""
    clear = build_clear(params, online=True)
    if not params.summary_target_fraction:
        strategy = clear
    else:
        strategy = TieredCompaction(
            tiers=[
                clear,
                SummarizingCompaction(
                    model=params.summary_model,
                    max_fraction=params.summary_target_fraction,
                    keep_messages=params.summary_keep_messages,
                ),
            ],
            target_fraction=params.summary_target_fraction,
            context_window=params.context_window,
        )
    return RecordingCompaction(strategy=strategy, params=params)
