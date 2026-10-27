"""决策消息历史：内存生效序列 + DB 全量持久化 + 截断摘要锚点"""

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from nonebot import logger
from pydantic_ai import Agent, ModelMessage, ModelRequest, ModelResponse, RunUsage
from pydantic_ai.messages import TextPart, UserPromptPart

from kanade_bot.utils.common import asia_shanghai_now
from kanade_bot.utils.pai_runtime import build_model_settings, get_model

from ...config import cfg
from ..history import estimate_request_tokens
from ..history.gate_store import GateStore
from ..prompt import _bot_name
from .gate import ReplyDecision, effective_gate_config, gate_limit_tokens, get_gate_agent

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


# ===== 每日清空（决策上下文的当日边界） =====


def daily_reset_boundary(at: datetime | None = None) -> datetime:
    """最近一次每日清空时刻：决策上下文的当日边界（Asia/Shanghai）

    决策上下文的“一天”以 `auto_reply.daily_reset_time`（默认 04:00）为界：
    到点清空内存生效序列，重启恢复只取该时刻之后的行。
    """
    at = at or asia_shanghai_now()
    reset = cfg.auto_reply.daily_reset_time
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
    每日定时清空内存生效序列（`reset_context`，时刻见 `daily_reset_boundary`），
    重启经 `restore_today` 只取当日行；跨清空时刻的前缀缓存失效属预期。
    """

    def __init__(self, store: GateStore, session_id: str):
        self.store = store
        self.session_id = session_id
        self.rows: list[GateRow] = []
        """生效序列（当前锚点在最前）"""
        self.watermark = 0
        """生效水位线（普通行起始 DB 行号）"""
        self.anchor_at: float | None = None
        """当前锚点生成时间（wall clock）"""

    async def restore(self, *, since: float | None = None) -> None:
        """从 DB 恢复生效序列与元信息（首次使用该会话时调用）

        :param since: 仅恢复该时刻（含）之后的行（锚点与普通行同受约束）；
            每日清空边界用，None 为全量口径。边界之前的行仍在库中，只是不进上下文
        """
        snapshot = await self.store.load_gate_history(self.session_id, since=since)
        self.rows = [GateRow(row_id, role, content) for row_id, role, content in snapshot.rows]
        self.watermark = snapshot.watermark
        self.anchor_at = snapshot.anchor_at
        if snapshot.rows:
            logger.debug(
                f"决策历史恢复 {self.session_id}：{len(self.rows)} 条生效行，"
                f"水位线 {self.watermark}，全量 {snapshot.total_rows} 行"
            )

    async def restore_today(self) -> None:
        """恢复当日决策上下文：自最近每日清空时刻起的行（重启默认口径）"""
        await self.restore(since=daily_reset_boundary().timestamp())

    def reset_context(self) -> None:
        """清空内存生效序列（每日定时清空用）

        DB 全量行与水位线保留不删：重启经 `restore_today` 只取当日行，
        更早的留库可回溯。旧锚点内容属清空前的上下文，锚点态一并丢弃。
        """
        self.rows = []
        self.anchor_at = None

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
