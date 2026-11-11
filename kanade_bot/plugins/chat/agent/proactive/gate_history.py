"""决策消息历史：内存生效序列 + DB 全量持久化"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta

from nonebot import logger
from pydantic_ai import ModelMessage, ModelRequest, ModelResponse, RunUsage
from pydantic_ai.messages import TextPart, UserPromptPart

from kanade_bot.utils.common import asia_shanghai_now

from ...config import cfg
from ..history import estimate_request_tokens
from ..history.gate_store import GateStore
from ..prompt import _bot_name
from .gate import ReplyDecision, gate_context_window, get_gate_agent

# ===== 每日清空（决策上下文的当日边界） =====


def daily_reset_boundary(at: datetime | None = None) -> datetime:
    """最近一次每日清空时刻：决策上下文的当日边界"""
    at = at or asia_shanghai_now()
    reset = cfg.proactive.daily_reset_time
    boundary = at.replace(hour=reset.hour, minute=reset.minute, second=0, microsecond=0)
    return boundary if boundary <= at else boundary - timedelta(days=1)


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

META_PROMPT_PREFIX = "待判定："
"""判定轮尾部元信息 user 行的固定前缀（护栏定位判定轮用，见 enforce_context_guard）"""


def build_meta_prompt(meta: GateMeta) -> str:
    """本次决策轮的 user 消息：待判定范围 + 时间元信息

    动态内容只出现在这条消息里（判定后随 assistant 判定一并固化进历史），
    系统提示与历史前缀保持逐字节稳定，前缀缓存可命中到上一轮判定为止。
    """
    name = _bot_name()
    lines = [
        f"{META_PROMPT_PREFIX}最近 {meta.new_messages} 条消息（自上次判定以来的新增）。",
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
    """DB 行号"""

    role: str
    """'user'（群消息/bot 发言）或 'assistant'（判定）"""

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

    内存 `rows` 即当前请求的 messages 主体；每次 append 同时写内存与 DB，
    重启按恢复口径加载普通行，请求前缀与关机前一致，前缀缓存跨重启命中。
    每日定时清空内存生效序列（`reset_context`，时刻见 `daily_reset_boundary`），
    重启经 `restore_today` 只取当日行；跨清空时刻的前缀缓存失效属预期。
    上下文规模由每日清空约束，超限由护栏兑底截断（`enforce_context_guard`），
    不做摘要压缩。
    """

    def __init__(self, store: GateStore, session_id: str):
        self.store = store
        self.session_id = session_id
        self.rows: list[GateRow] = []
        """生效序列"""

    async def restore(self, *, since: float | None = None) -> None:
        """从 DB 恢复生效序列与元信息（首次使用该会话时调用）

        :param since: 仅恢复该时刻（含）之后的行；
            每日清空边界用，None 为全量口径。边界之前的行仍在库中，只是不进上下文
        """
        snapshot = await self.store.load_gate_history(self.session_id, since=since)
        self.rows = [GateRow(row_id, role, content) for row_id, role, content in snapshot.rows]
        if snapshot.rows:
            logger.debug(
                f"决策历史恢复 {self.session_id}：{len(self.rows)} 条生效行，"
                f"全量 {snapshot.total_rows} 行"
            )

    async def restore_today(self) -> None:
        """恢复当日决策上下文：自最近每日清空时刻起的行（重启默认口径）"""
        await self.restore(since=daily_reset_boundary().timestamp())

    def reset_context(self) -> None:
        """清空内存生效序列（每日定时清空用）

        DB 全量行保留不删：重启经 `restore_today` 只取当日行，
        更早的留库可回溯。
        """
        self.rows = []

    async def clear(self) -> int:
        """清空决策历史：内存生效序列与 DB 全量行

        决策器从零开始重新旁观，新消息照常 append；时间戳元信息 kv
        （last_bot_at 等）不在清理范围。返回删除的 DB 行数。
        """
        deleted = await self.store.clear_gate_history(self.session_id)
        self.rows = []
        return deleted

    async def append(self, role: str, content: str) -> None:
        """追加一条普通行（群消息/判定/kanade 发言），内存与 DB 同步"""
        (row_id,) = await self.store.append_gate_messages(self.session_id, [(role, content)])
        self.rows.append(GateRow(row_id, role, content))

    def model_messages(self) -> list[ModelMessage]:
        """生效序列 → pydantic-ai 原生消息列表"""
        return [GateRow.to_model_message(row) for row in self.rows]

    def context_stats(self) -> tuple[int, int]:
        """（生效条数, 输入 token 估算）"""
        return len(self.rows), estimate_request_tokens(self.model_messages())

    def enforce_context_guard(self) -> int:
        """护栏：估算输入 tokens 超决策模型窗口的 `context_usage_threshold` 比例时，
        截断内存生效序列，保留最近 `context_keep_decisions` 个完整判定轮

        窗口解析失败或阈值 ≤ 0 时护栏关闭。返回截除的行数（未触发返回 0）。
        """
        threshold = cfg.proactive.context_usage_threshold
        keep = cfg.proactive.context_keep_decisions
        window = gate_context_window()
        if window is None or window <= 0 or threshold <= 0 or keep <= 0 or not self.rows:
            return 0
        limit = int(window * threshold)
        _, tokens = self.context_stats()
        if tokens <= limit:
            return 0
        meta_rows = [
            index
            for index, row in enumerate(self.rows)
            if row.role == "user" and row.content.startswith(META_PROMPT_PREFIX)
        ]
        assistant_rows = [index for index, row in enumerate(self.rows) if row.role == "assistant"]
        if not meta_rows or not assistant_rows:
            logger.warning(
                f"决策上下文护栏未生效 {self.session_id}：估算 {tokens} 超限 {limit}，"
                f"但生效序列中没有判定轮可定位，保留全部"
            )
            return 0
        if len(assistant_rows) > keep:
            # 最近 keep 个完整判定轮：自倒数第 keep+1 个判定行的下一行起保留
            # （即倒数第 keep 轮的首条消息）
            cut = assistant_rows[-keep - 1] + 1
        else:
            cut = meta_rows[0]
        if cut <= 0:
            logger.warning(
                f"决策上下文护栏未生效 {self.session_id}：估算 {tokens} 超限 {limit}，"
                f"但首个判定轮前无更早行可截，保留全部"
            )
            return 0
        dropped = self.rows[:cut]
        self.rows = self.rows[cut:]
        kept_rounds = min(keep, len(assistant_rows))
        logger.warning(
            f"决策上下文护栏触发 {self.session_id}：估算 {tokens} 超限 {limit}"
            f"（窗口 {window} × {threshold:.0%}），"
            f"截除最旧 {len(dropped)} 行（保留最近 {kept_rounds} 个判定轮）"
        )
        return len(dropped)

    async def decide(self, meta: GateMeta, *, timeout: float) -> GateOutcome | None:
        """发起一次决策：LLM 调用 → 固化本轮 user/assistant 行

        失败（超时/异常/输出异常）返回 None，由调用方按「失败即放弃」处理
        （不回、不计退避）；成功时本轮尾部元信息与判定已 append 进历史。
        """
        try:
            self.enforce_context_guard()
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
