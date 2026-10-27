"""主动回复 LLM 决策节点（reply gate）

在主 Agent 之前判断「此刻是否值得主动插话」：

- 输入 = 固定系统提示 + 决策消息历史（原生 message 序列，append-only，
  持久化 per-session SQLite）+ 本次决策轮的尾部元信息；
- 输出 = `ReplyDecision(reply, reason)`，二分类加一句理由；
- 上下文超限时间戳头压缩为一条摘要锚点消息（复用 `cfg.compaction`
  摘要档配置与 token 估算），水位线推进、全量行保留可回溯。
"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime

from nonebot import logger
from pydantic import BaseModel
from pydantic_ai import Agent, ModelMessage, ModelRequest, ModelResponse, RunUsage
from pydantic_ai.messages import TextPart, UserPromptPart

from kanade_bot.utils.billing import is_non_workday
from kanade_bot.utils.common import asia_shanghai_now
from kanade_bot.utils.pai_runtime import (
    build_model_settings,
    get_model,
    resolve_model_context_window,
)
from kanade_bot.utils.schema import BaseAgentConfig

from ..config import TimeWindow, cfg
from .compaction import estimate_request_tokens
from .prompt import PLACEHOLDER_PATTERN, _bot_name
from .session_store import SessionStore

GATE_SYSTEM_PROMPT = """你是{{bot_name}}的「插话决策器」，用于判断{{bot_name}}此刻是否应该主动插话。
下面是你在旁观的群聊记录（含你过去的判定）。
输出格式 JSON：{"reply": bool, "reason": "一句理由"}"""
"""内置默认插话决策提示词，`{{bot_name}}` 占位符渲染时替换为 Bot 昵称"""

ANCHOR_PREFIX = "$ 以下是你之前旁观的群聊记录摘要"
"""摘要锚点消息的固定前缀（锚点内容由此开头，便于辨识）"""

FALLBACK_SUMMARY_TEMPLATE = (
    "请把下面 <messages> 标签内的群聊旁观记录压缩成一份简洁的中文摘要，"
    "保留：参与者、话题脉络、你（插话决策器）已作出的判定、未尽事项。"
    "直接输出摘要正文，不要前言或代码围栏。\n\n<messages>\n{messages}\n</messages>"
)
"""摘要模板文件缺失/非法时的内置回退模板"""

SUMMARY_TIMEOUT = 120.0
"""摘要锚点生成调用超时（秒）"""


class ReplyDecision(BaseModel):
    """决策节点结构化输出"""

    reply: bool
    """是否主动插话"""

    reason: str
    """一句理由"""


# ===== 模型解析（进程级缓存） =====

_gate_agent: Agent[None, ReplyDecision] | None = None
"""决策 Agent 进程级单例：系统提示与 output schema 恒定，保证前缀缓存命中"""

_context_window: int | None = None
_context_window_resolved = False


def effective_gate_config() -> BaseAgentConfig:
    """决策节点生效模型配置：`gate` 留空（未配 model）时回退主模型 `cfg.agent`"""
    gate = cfg.auto_reply.gate
    return gate if gate.model else cfg.agent


def _substitute_bot_name(template: str) -> str:
    """把模板中的 `{{ bot_name }}` 占位符替换为 Bot 昵称（`_bot_name()`）

    昵称来自 nonebot `nickname` 配置，进程内恒定，不影响前缀缓存。
    """
    return PLACEHOLDER_PATTERN.sub(
        lambda m: _bot_name() if m.group(1) == "bot_name" else m.group(0), template
    )


def gate_system_prompt() -> str:
    """插话决策系统提示词：`gate.system_prompt_file` 配置文件优先，回退内置默认

    `{{ bot_name }}` 占位符替换为 Bot 昵称；进程内首次构建 Agent 时读一次
    （提示词须逐字节稳定以保前缀缓存命中），后续修改文件需重启生效。
    """
    path = cfg.auto_reply.gate.system_prompt_file_path
    if path.is_file():
        if text := path.read_text(encoding="utf-8").strip():
            return _substitute_bot_name(text)
        logger.warning(f"决策系统提示词文件为空，使用内置默认：{path}")
    else:
        logger.warning(f"决策系统提示词文件不存在，使用内置默认：{path}")
    return _substitute_bot_name(GATE_SYSTEM_PROMPT)


def get_gate_agent() -> Agent[None, ReplyDecision]:
    """构建（或返回缓存的）决策 Agent"""
    global _gate_agent
    if _gate_agent is None:
        config = effective_gate_config()
        _gate_agent = Agent(
            name="kanade-bot-reply-gate",
            instructions=gate_system_prompt(),
            model=get_model(config),
            model_settings=build_model_settings(config),
            output_type=ReplyDecision,
        )
    return _gate_agent


def gate_context_window() -> int | None:
    """决策模型的上下文窗口（显式配置 > /models 元数据 > genai-prices），进程内固化"""
    global _context_window, _context_window_resolved
    if not _context_window_resolved:
        try:
            _context_window = resolve_model_context_window(effective_gate_config())
        except Exception as e:
            logger.warning(f"决策模型上下文窗口解析失败（截断阈值不可用）：{e}")
            _context_window = None
        _context_window_resolved = True
    return _context_window


def gate_limit_tokens() -> int | None:
    """决策上下文触发上限：窗口 × `summary_target_fraction`（None 回退 `trigger_fraction`）"""
    window = gate_context_window()
    if window is None:
        return None
    fraction = cfg.compaction.summary_target_fraction or cfg.compaction.trigger_fraction
    return max(1, int(window * fraction))


# ===== 时段概率窗口匹配 =====


def time_in_window(current, start, end) -> bool:
    """时刻是否落在 `[start, end)` 半开区间内，支持跨零点（23:00 → 07:00）"""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def match_time_window(at: datetime | None = None) -> TimeWindow | None:
    """命中的时段窗口：多个命中取列表中最先声明者（自上而下即优先级）

    都不命中时返回 None（时段外每批必送，概率视为 1.0）。
    """
    at = at or asia_shanghai_now()
    current = at.time()
    for window in cfg.auto_reply.time_windows:
        if window.weekdays_only and is_non_workday(at):
            continue
        if time_in_window(current, window.start, window.end):
            return window
    return None


def window_probability(at: datetime | None = None) -> tuple[float, TimeWindow | None]:
    """当前时刻的送审概率与命中窗口；无窗口命中时概率为 1.0"""
    window = match_time_window(at)
    return (window.probability if window else 1.0), window


def describe_window(window: TimeWindow | None) -> str:
    """窗口的简短描述（日志与统计用）"""
    if window is None:
        return "无窗口命中"
    desc = f"{window.start:%H:%M}-{window.end:%H:%M}"
    if window.weekdays_only:
        desc += " 工作日"
    return desc


# ===== 本次决策轮的尾部元信息（唯一变化部分） =====


@dataclass
class GateMeta:
    """构建决策轮 user 消息的动态元信息"""

    new_messages: int
    """自上次判定以来的新增群消息条数"""

    now: datetime
    """当前北京时间"""

    since_bot_s: float | None
    """kanade 上次发言距今秒数；None 表示未知（刚启动且从未发言）"""

    since_proactive_s: float | None
    """上次主动回复距今秒数；None 表示从未主动回复"""

    silence_streak: int
    """连续决策不回次数（退避计数同源）"""


def format_duration(seconds: float) -> str:
    """把秒数格式化为人类可读时长（元信息展示用）"""
    if seconds < 60:
        return f"{seconds:.0f}秒"
    if seconds < 3600:
        return f"{seconds / 60:.0f}分钟"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}小时"
    return f"{seconds / 86400:.1f}天"


_WEEKDAY_ZH = "一二三四五六日"


def build_meta_prompt(meta: GateMeta) -> str:
    """本次决策轮的 user 消息：待判定范围 + 时间元信息

    动态内容只出现在这条消息里（判定后随 assistant 判定一并固化进历史），
    系统提示与历史前缀保持逐字节稳定，前缀缓存可命中到上一轮判定为止。
    """
    name = _bot_name()
    lines = [
        f"待判定：最近 {meta.new_messages} 条消息（自上次判定以来的新增）。",
        f"当前时间：{meta.now:%m-%d %H:%M}（周{_WEEKDAY_ZH[meta.now.weekday()]}）。",
        (
            f"{name} 上次发言距今：{format_duration(meta.since_bot_s)}。"
            if meta.since_bot_s is not None
            else f"{name} 上次发言距今：未知（启动后还未发言）。"
        ),
        (
            f"上次主动回复距今：{format_duration(meta.since_proactive_s)}。"
            if meta.since_proactive_s is not None
            else "上次主动回复距今：从未。"
        ),
        f"连续沉默次数（近期决策均为不回）：{meta.silence_streak}。",
        f"请判断 {name} 此刻是否应该主动插话。",
    ]
    return "\n".join(lines)


# ===== 决策消息历史 =====


@dataclass
class GateRow:
    """决策消息历史中的一行（内存形态，与 DB 行一一对应）"""

    row_id: int
    """DB 行号（锚点行为其锚点行 id）"""

    role: str
    """'user'（群消息/kanade 发言/摘要锚点）或 'assistant'（判定）"""

    content: str
    """消息文本；判定行为 `{"reply": ..., "reason": ...}` JSON"""

    @staticmethod
    def to_model_message(row: "GateRow") -> ModelMessage:
        """转换为 pydantic-ai 原生消息（user→ModelRequest，assistant→ModelResponse）"""
        if row.role == "assistant":
            return ModelResponse(parts=[TextPart(content=row.content)])
        return ModelRequest(parts=[UserPromptPart(content=row.content)])


@dataclass
class GateOutcome:
    """一次决策调用的结果"""

    decision: ReplyDecision
    usage: RunUsage


class SessionGateHistory:
    """单会话决策消息历史：内存生效序列 + DB 全量持久化

    内存 `rows` 即当前请求的 messages 主体（锚点在前）；每次 append 同时
    写内存与 DB。截断（`_truncate`）保留最近 N 条原始行、把更早部分压缩成
    新锚点行，DB 不删行只推进水位线——重启加载 `[最新锚点] + [水位线后普通行]`，
    请求前缀与关机前一致，前缀缓存跨重启命中。
    """

    def __init__(self, store: SessionStore, session_id: str):
        self.store = store
        self.session_id = session_id
        self.rows: list[GateRow] = []
        """生效序列（当前锚点在最前）"""
        self.watermark = 0
        """生效水位线（普通行起始 DB 行号）"""
        self.anchor_at: float | None = None
        """当前锚点生成时间（wall clock）"""

    async def restore(self) -> None:
        """从 DB 恢复生效序列与元信息（首次使用该会话时调用）"""
        snapshot = await self.store.load_gate_history(self.session_id)
        self.rows = [GateRow(row_id, role, content) for row_id, role, content in snapshot.rows]
        self.watermark = snapshot.watermark
        self.anchor_at = snapshot.anchor_at
        if snapshot.rows:
            logger.debug(
                f"决策历史恢复 {self.session_id}：{len(self.rows)} 条生效行，"
                f"水位线 {self.watermark}，全量 {snapshot.total_rows} 行"
            )

    async def clear(self) -> int:
        """清空决策历史：内存生效序列与 DB 全量行（含锚点行与水位线）

        决策器从零开始重新旁观，新消息照常 append；时间戳元信息 kv
        （last_bot_at 等）不在清理范围。返回删除的 DB 行数。
        """
        deleted = await self.store.clear_gate_history(self.session_id)
        self.rows = []
        self.watermark = 0
        self.anchor_at = None
        return deleted

    async def append(self, role: str, content: str) -> None:
        """追加一条普通行（群消息/判定/kanade 发言），内存与 DB 同步"""
        (row_id,) = await self.store.append_gate_messages(self.session_id, [("msg", role, content)])
        self.rows.append(GateRow(row_id, role, content))

    def model_messages(self) -> list[ModelMessage]:
        """生效序列 → pydantic-ai 原生消息列表"""
        return [GateRow.to_model_message(row) for row in self.rows]

    def context_stats(self) -> tuple[int, int]:
        """（生效条数, 输入 token 估算）"""
        return len(self.rows), estimate_request_tokens(self.model_messages())

    async def decide(self, meta: GateMeta, *, timeout: float) -> GateOutcome | None:
        """发起一次决策：截断检查 → LLM 调用 → 固化本轮 user/assistant 行

        失败（超时/异常/输出异常）返回 None，由调用方按「失败即放弃」处理
        （不回、不计退避）；成功时本轮尾部元信息与判定已 append 进历史。
        """
        try:
            await self._maybe_truncate()
            history = self.model_messages()
            prompt = build_meta_prompt(meta)
            result = await asyncio.wait_for(
                get_gate_agent().run(prompt, message_history=history),
                timeout=timeout,
            )
            decision = result.output
            if not isinstance(decision, ReplyDecision):
                raise RuntimeError(f"决策输出类型异常：{type(decision)}")  # noqa: TRY004
            # 固化：本次决策轮 user（元信息）+ assistant（判定，稳定 JSON 形态）
            await self.append("user", prompt)
            await self.append(
                "assistant",
                json.dumps(
                    {"reply": decision.reply, "reason": decision.reason},
                    ensure_ascii=False,
                ),
            )
            return GateOutcome(decision=decision, usage=result.usage)
        except Exception as e:
            logger.warning(f"主动回复决策调用失败 {self.session_id}：{e}")
            return None

    # ===== 截断：截头 + 摘要锚点（复用 compaction 摘要档配置） =====

    async def _maybe_truncate(self) -> None:
        """每次决策发起前检查：估算超上限则截头压缩为摘要锚点"""
        limit = gate_limit_tokens()
        if limit is None:
            return
        estimated = estimate_request_tokens(self.model_messages())
        if estimated <= limit:
            return
        await self._truncate(estimated, limit)

    async def _truncate(self, estimated: int, limit: int) -> None:
        compaction = cfg.compaction
        keep = compaction.summary_keep_messages
        if len(self.rows) <= keep:
            # 全部行都在保留尾部内，无可截内容：照原样发起请求（罕见，交由端点处理）
            logger.warning(
                f"决策历史 {self.session_id} 估算 {estimated} tokens 超上限 {limit}，"
                f"但 {len(self.rows)} 条均不超过保留条数 {keep}，本次不截断"
            )
            return

        dropped, kept = self.rows[:-keep], self.rows[-keep:]
        summary = await self._summarize(dropped)
        anchor_content = f"{ANCHOR_PREFIX}，{len(dropped)} 条记录压缩）\n{summary}"
        (anchor_id,) = await self.store.append_gate_messages(
            self.session_id, [("anchor", "user", anchor_content)]
        )
        await self.store.set_gate_state(self.session_id, "watermark", str(kept[0].row_id))
        self.rows = [GateRow(anchor_id, "user", anchor_content), *kept]
        self.watermark = kept[0].row_id
        self.anchor_at = time.time()
        after = estimate_request_tokens(self.model_messages())
        logger.info(
            f"决策历史截断 {self.session_id}：{len(dropped) + keep} 条 → "
            f"1 锚点 + {len(kept)} 条，估算 {estimated} → {after} tokens（上限 {limit}）"
        )

    async def _summarize(self, dropped: list[GateRow]) -> str:
        """把被截行压缩为摘要文本：复用 compaction 摘要模板与摘要模型配置"""
        template = self._load_summary_template()
        rendered = "\n".join(
            f"{'User' if row.role == 'user' else 'Assistant'}: {row.content}" for row in dropped
        )
        prompt = template.format(messages=rendered)

        base = effective_gate_config()
        model_id = cfg.compaction.summary_model or base.model
        summary_config = base.model_copy(update={"model": model_id})
        agent: Agent[None, str] = Agent(
            name="kanade-bot-reply-gate-summary",
            model=get_model(summary_config),
            model_settings=build_model_settings(summary_config),
            output_type=str,
        )
        result = await asyncio.wait_for(agent.run(prompt), timeout=SUMMARY_TIMEOUT)
        text = result.output.strip() if isinstance(result.output, str) else ""
        if not text:
            raise RuntimeError("摘要锚点生成为空")
        return text

    def _load_summary_template(self) -> str:
        """读摘要模板文件（含 `{messages}` 占位校验）；缺失或非法时回退内置模板"""
        path = cfg.compaction.summary_prompt_file_path
        if path.is_file():
            template = path.read_text(encoding="utf-8")
            if template.strip() and "{messages}" in template:
                try:
                    template.format(messages="")
                    return template
                except (KeyError, IndexError, ValueError):
                    pass
            logger.warning(f"决策摘要模板非法，回退内置模板：{path}")
        return FALLBACK_SUMMARY_TEMPLATE
