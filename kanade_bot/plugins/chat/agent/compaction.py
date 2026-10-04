"""会话压缩（Pydantic AI Harness S3 策略 + 压缩事件落库）。

设计要点（与 `MIGRATION_PLAN_PYDANTIC_AI.md` 4.5 节一致）：

- **不用滑动窗口**：本项目 KV Cache 命中率关键，滑动窗口每轮移动前缀起点会让
  provider 侧前缀缓存全部失效。`ClearToolResults` 只清空旧工具结果的**内容**，
  消息结构与位置不变，前缀稳定。
- **清理收益下限用绝对 token 数**（`min_clear_tokens`）：清理会改写内容、使该点
  之后的 prompt cache 失效，收益太小就跳过，避免白白弄坏缓存。
- **压缩事件落库**：在线压缩后写一条 `CompactionMark`；恢复时按 marks 重放，
  保证重建的历史与关闭前最后一次请求发送的内容一致。
- **在线压缩与恢复重放共用同一个 `apply_strategy`**：这是本设计最重要的
  不变式，两套代码必然漂移，一漂移前缀就变、缓存就失效。

## 落库策略：自验证的「能重放就只存参数」

每轮结束时对 mark 做一次**自验证**：

1. 用零成本档从「本轮首次压缩前的历史」重放一次；
2. 若重放结果与在线压缩后的历史**完全一致** ⇒ 记 `clear` 档（只存参数，
   恢复时重放即可，零额外存储）；
3. 否则说明触发了摘要档（LLM 非确定性）⇒ 记 `summarizing` 档并存完整快照。

这样「能不能重放」不靠猜，而是每轮实测；也保证两条路径共用同一份代码。

## 实测结论（本轮验证）

- `ClearToolResults` 是**确定性 + 幂等**的纯函数：相同输入 + 相同 `keep_pairs`
  ⇒ 相同输出；重复应用不改变结果（已清理的 part 不会被再次触碰）。
- 从 DB 全量原始消息重放 ⇒ 与在线压缩结果**逐字节一致**。
- 重放时必须**忽略 `min_clear_tokens`**：该阈值依赖 provider 上报的 usage 锚点
  估计 token，重放时没有同样的锚点；mark 已记录「确实压缩过」，故无条件执行。
- Pydantic AI 在每次请求前对历史做 `repair_messages()`（合并相邻请求、补全孤儿
  工具结果），对比历史时必须先归一化，否则会看到假差异。
"""

import time
from dataclasses import dataclass, field
from typing import Any, Self, TypeVar

from pydantic_ai.capabilities.abstract import AbstractCapability
from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
from pydantic_ai.models import Model, ModelRequestContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai_harness.compaction import (
    ClearToolResults,
    SummarizingCompaction,
    TieredCompaction,
)

AgentDepsT = TypeVar("AgentDepsT")

STRATEGY_CLEAR = "clear_tool_results"
"""零成本档：清空旧工具结果。确定性 + 幂等，可由参数重放"""

STRATEGY_SUMMARIZE = "summarizing"
"""摘要档：调 LLM 生成摘要。非确定性，必须存产物"""


@dataclass
class CompactionParams:
    """压缩参数（必须持久化，恢复时用记录值而非当前配置）"""

    trigger_fraction: float = 0.8
    """触发清理的上下文占用比例（按模型真实上下文窗口解析）

    用比例而非绝对 token 数：一个配置对所有模型都正确，
    换个模型也不必重新校准。"""

    keep_pairs: int = 3
    """`ClearToolResults` 保留的最近工具调用对数"""

    min_clear_tokens: int = 2_000
    """清理收益低于此 token 数则跳过（保护 prompt cache）；**仅在线生效**"""

    context_window: int | None = None
    """上下文窗口覆盖值；None 时按模型 profile / genai-prices 解析"""

    summary_target_fraction: float | None = None
    """`TieredCompaction` 的停止预算（上下文占用比例）；None 表示不启用摘要档"""

    summary_model: str | None = None
    """摘要使用的模型 ID；None 表示继承主模型"""

    summary_keep_messages: int = 40
    """生成摘要时保留的最近消息条数"""

    def fingerprint(self) -> str:
        """参数指纹：用于检测配置漂移"""
        return (
            f"{self.trigger_fraction}/{self.keep_pairs}/{self.min_clear_tokens}/"
            f"{self.context_window}/{self.summary_target_fraction}/"
            f"{self.summary_model}/{self.summary_keep_messages}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "trigger_fraction": self.trigger_fraction,
            "keep_pairs": self.keep_pairs,
            "min_clear_tokens": self.min_clear_tokens,
            "context_window": self.context_window,
            "summary_target_fraction": self.summary_target_fraction,
            "summary_model": self.summary_model,
            "summary_keep_messages": self.summary_keep_messages,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Self:
        return cls(
            trigger_fraction=float(data.get("trigger_fraction", 0.8)),
            keep_pairs=int(data.get("keep_pairs", 3)),
            min_clear_tokens=int(data.get("min_clear_tokens", 2_000)),
            context_window=data.get("context_window"),
            summary_target_fraction=data.get("summary_target_fraction"),
            summary_model=data.get("summary_model"),
            summary_keep_messages=int(data.get("summary_keep_messages", 40)),
        )


def build_clear(params: CompactionParams, *, online: bool) -> ClearToolResults:
    """构造零成本档策略

    `online=False`（恢复重放）时**忽略 `min_clear_tokens`**：该阈值依赖 provider
    上报的 usage 锚点估计 token，重放时没有同样的锚点。mark 记录的是
    「确实清理过」这个事实，所以重放无条件执行。
    """

    return ClearToolResults(
        max_fraction=params.trigger_fraction,
        context_window=params.context_window,
        keep_pairs=params.keep_pairs,
        min_clear_tokens=params.min_clear_tokens if online else None,
    )


def build_strategy(params: CompactionParams) -> ClearToolResults | TieredCompaction:
    """构造在线压缩策略

    - `summary_target_fraction` 未配置（默认）⇒ 只用零成本的 `ClearToolResults`，
      不启用摘要档（也就没有 LLM 非确定性风险，marks 永远可重放）；
    - 配置了它 ⇒ `TieredCompaction([Clear, Summarizing])`，
      先跑零成本档，仍超预算才升级到摘要。
    """
    clear = build_clear(params, online=True)
    if not params.summary_target_fraction:
        return clear

    return TieredCompaction(
        tiers=[
            clear,
            # `SummarizingCompaction` 必须给出触发阈值（max_tokens / max_messages /
            # max_fraction 三选一），否则构造即报错。`TieredCompaction` 绕过 tier 自身的
            # 触发条件、直接驱动 `compact()`，这里取与停止预算相同的比例。
            SummarizingCompaction(
                model=params.summary_model,
                max_fraction=params.summary_target_fraction,
                keep_messages=params.summary_keep_messages,
            ),
        ],
        target_fraction=params.summary_target_fraction,
        context_window=params.context_window,
    )


@dataclass
class CompactionMark:
    """一条压缩事件：记录「历史已按 strategy/params 压缩过」"""

    strategy: str
    """`clear_tool_results`（可重放）或 `summarizing`（需产物）"""

    params: dict[str, Any] = field(default_factory=dict)
    """压缩参数（重放时用记录值，不用当前配置）"""

    result: bytes | None = None
    """不可重放策略的产物（压缩后完整历史的 JSON），可重放策略为 None"""

    fingerprint: str = ""
    """参数指纹，用于检测配置漂移"""

    applied_at: float = 0.0
    """记录时间戳"""


def fingerprint(messages: list[ModelMessage]) -> bytes:
    """消息列表指纹（判断压缩是否真的改变了内容）"""
    return ModelMessagesTypeAdapter.dump_json(list(messages))


async def apply_strategy(
    messages: list[ModelMessage],
    mark: CompactionMark,
    *,
    model: Model | str | None = None,
) -> list[ModelMessage]:
    """把一条压缩事件应用到消息列表上

    **在线压缩与恢复重放都必须经过本函数**（设计不变式）：
    参数一律取自 `mark.params`，不用当前配置。
    """
    params = CompactionParams.from_json(mark.params)

    if mark.result is not None:
        # 摘要档：LLM 非确定性，直接用存下来的产物，不重新生成
        return ModelMessagesTypeAdapter.validate_json(mark.result)

    # 零成本档：确定性 + 幂等的纯函数，用记录参数重跑
    from pydantic_ai_harness.compaction import compact_now

    strategy = build_clear(params, online=False)
    return await compact_now(
        strategy, list(messages), model=model if model is not None else TestModel()
    )


@dataclass
class RecordingCompaction(AbstractCapability[AgentDepsT]):
    """包住分层压缩策略，并记录「本轮历史是否被压缩过」

    落库由调用方（会话管理器）在每轮结束时调用 `take_mark()` 统一做
    （一轮最多一条 mark），避免 run 中途写库导致 mark 与消息写入顺序错乱。
    """

    strategy: AbstractCapability[AgentDepsT]
    """在线压缩策略（capability）：`summary_target_fraction` 为空时是
    `ClearToolResults`，否则是 `TieredCompaction([ClearToolResults, SummarizingCompaction])`"""

    params: CompactionParams
    """当前压缩参数（写进 mark 供重放）"""

    compacted: bool = False
    """本轮是否发生过压缩"""

    pre_compaction: list[ModelMessage] | None = None
    """本轮首次压缩前的历史快照（用于自验证能否重放）"""

    async def before_model_request(
        self,
        ctx: RunContext[AgentDepsT],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        before = list(ctx.messages)
        result = await self.strategy.before_model_request(ctx, request_context)
        if fingerprint(before) != fingerprint(list(ctx.messages)):
            self.compacted = True
            if self.pre_compaction is None:
                self.pre_compaction = before
        return result

    async def take_mark(self, final_history: list[ModelMessage]) -> CompactionMark | None:
        """本轮结束后取出一条 mark（无压缩则返回 None），并重置状态

        自验证：先试零成本档重放，能复现就只存参数（零额外存储），
        否则说明触发了摘要档，必须存完整快照。
        """
        compacted, self.compacted = self.compacted, False
        pre, self.pre_compaction = self.pre_compaction, None
        if not compacted or pre is None:
            return None

        target = fingerprint(final_history)
        candidate = CompactionMark(
            strategy=STRATEGY_CLEAR,
            params=self.params.to_json(),
            result=None,
            fingerprint=self.params.fingerprint(),
            applied_at=time.time(),
        )
        if fingerprint(await apply_strategy(pre, candidate)) == target:
            return candidate

        return CompactionMark(
            strategy=STRATEGY_SUMMARIZE,
            params=self.params.to_json(),
            result=target,
            fingerprint=self.params.fingerprint(),
            applied_at=time.time(),
        )


def build_compaction_capability(params: CompactionParams | None = None) -> RecordingCompaction:
    """构造压缩 capability

    注册顺序约束：**必须排在 `PrepareTools` 之前**（压缩 → 工具过滤）。
    """
    p = params or CompactionParams()
    return RecordingCompaction(strategy=build_strategy(p), params=p)


def build_clear_mark(params: CompactionParams) -> CompactionMark:
    """按当前参数构造一条零成本档 mark（参数漂移后从全量重放用）"""
    return CompactionMark(
        strategy=STRATEGY_CLEAR,
        params=params.to_json(),
        result=None,
        fingerprint=params.fingerprint(),
        applied_at=time.time(),
    )
