"""主动回复运行时编排：防抖 → 退避 → 时段概率闸 → LLM 决策 → 回复执行"""

import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic

from nonebot import logger, require
from nonebot.adapters import Bot

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

from kanade_bot.utils.common import PlatformType, asia_shanghai_now, asia_shanghai_now_str
from kanade_bot.utils.session import SessionInfo

from ...config import cfg, chat_configs
from ...util import is_banned
from ..manager import chat_manager
from ..prompt import _bot_name
from .debounce import MessageDebouncer
from .executor import send_auto_reply
from .gate_history import GateMeta, SessionGateHistory, format_duration
from .windows import describe_window, window_probability


@dataclass
class GateCounters:
    """内存态统计计数器（重启清零）"""

    replies: int = 0
    """决策输出「回」并执行主动回复的次数"""

    silences: int = 0
    """决策输出「不回」的次数"""

    skipped: int = 0
    """时段概率闸未抽中（批次跳过）的次数"""

    failures: int = 0
    """决策调用失败（超时/异常）的次数"""

    input_tokens: int = 0
    """决策调用累计输入 tokens"""

    cache_read_tokens: int = 0
    """决策调用累计缓存命中 tokens"""


@dataclass
class SessionGateState:
    """单会话主动回复运行时状态"""

    session_id: str
    group_id: str
    history: SessionGateHistory
    debouncer: MessageDebouncer = field(default_factory=MessageDebouncer)
    counters: GateCounters = field(default_factory=GateCounters)

    bot: Bot | None = None
    """最近一次观察到的 Bot 实例（主动发送用）"""

    session_info: SessionInfo | None = None
    """最近一次观察到的会话信息"""

    timer: asyncio.Task | None = None
    """防抖到期唤醒任务"""

    backoff_timer: asyncio.Task | None = None
    """退避延迟重查任务"""

    backoff_count: int = 0
    """连续决策不回计数（决策回即清零）"""

    gate_running: bool = False
    """决策任务防重入标记：同一会话同时最多一个决策任务"""

    new_messages: int = 0
    """自上次判定以来的新增群消息条数"""

    last_bot_at: float | None = None
    """Bot上次发言时刻（wall clock 秒）"""

    last_proactive_at: float | None = None
    """上次主动回复时刻（wall clock 秒）"""

    load_task: asyncio.Task | None = None
    """首次使用时的决策历史恢复任务"""


def _format_k(tokens: int) -> str:
    """token 数值的 k 缩写（统计展示用）"""
    return f"{tokens / 1000:.1f}k"


class AutoReplyRuntime:
    """主动回复运行时单例：消息观察入口、决策编排与统计"""

    def __init__(self):
        self._states: dict[str, SessionGateState] = {}
        # 每日定时清空决策上下文（时刻随配置，进程启动时读取，改动需重启生效）
        reset = cfg.auto_reply.daily_reset_time
        scheduler.add_job(
            self._daily_reset,
            "cron",
            hour=reset.hour,
            minute=reset.minute,
            timezone="Asia/Shanghai",
            id="_daily_reset",
        )

    # ===== 启用判定 =====

    def is_enabled(self, group_id: str, platform: PlatformType) -> bool:
        """该群是否启用主动回复（总开关 + 运行时启用群列表 + 群黑名单）"""
        if not cfg.auto_reply.enabled:
            return False
        group_config = chat_configs.instance.get_by_platform(platform)
        if group_config is None or group_id not in group_config.auto_reply_enabled_groups:
            return False
        return not is_banned(group_id, "group", platform)

    def _still_enabled(self, state: SessionGateState) -> bool:
        """运行中复查（配置与黑名单可能动态变化），并要求 bot 实例可用"""
        platform = state.session_info.platform if state.session_info else None
        return (
            platform is not None
            and self.is_enabled(state.group_id, platform)
            and state.bot is not None
        )

    # ===== 会话状态 =====

    async def _ensure_state(self, session_id: str, group_id: str) -> SessionGateState:
        state = self._states.get(session_id)
        if state is None:
            state = SessionGateState(
                session_id=session_id,
                group_id=group_id,
                history=SessionGateHistory(chat_manager.gate_store, session_id),
            )
            state.load_task = asyncio.create_task(self._restore_state(state))
            self._states[session_id] = state
            # buffer 消费回调挂钩：被动回复消费 buffer 后重置防抖批次
            chat_manager.get_session(session_id).on_buffer_consumed = self.on_buffer_consumed
        if state.load_task is not None:
            await state.load_task
            state.load_task = None
        return state

    async def _restore_state(self, state: SessionGateState) -> None:
        """决策历史与元信息恢复（失败降级为空历史继续运行）"""
        session_id = state.session_id
        try:
            # 仅恢复当日上下文（自最近每日清空时刻起）：
            # 更早的全量行留库可回溯，不进上下文（决策器上下文过长且无用）
            await state.history.restore_today()
            store = state.history.store
            if (v := await store.get_gate_state(session_id, "last_bot_at")) is not None:
                state.last_bot_at = float(v)
            if (v := await store.get_gate_state(session_id, "last_proactive_at")) is not None:
                state.last_proactive_at = float(v)
        except Exception as e:
            logger.warning(f"决策历史恢复失败 {session_id}（按空历史继续）：{e}")

    # ===== 消息入口 =====

    async def on_group_message(
        self,
        session_id: str,
        group_id: str,
        text: str,
        *,
        author: str,
        bot: Bot,
        session_info: SessionInfo,
    ) -> None:
        """一条群消息进入主动回复流水线：append 决策历史 + 防抖观察

        整体容错：任何异常只记日志，不影响消息进 buffer 的主链路。
        """
        try:
            state = await self._ensure_state(session_id, group_id)
            state.bot = bot
            state.session_info = session_info
            await state.history.append("user", text)
            state.new_messages += 1
            state.debouncer.observe(author)
            self._schedule_timer(session_id, state)
        except Exception as e:
            logger.opt(exception=e).warning(f"主动回复消息观察失败 {session_id}")

    # ===== bot 发言入口（被动/主动产出 append） =====

    async def on_bot_message(self, session_id: str, content: str, *, proactive: bool = False):
        """kanade 发言以 user 消息 append 进决策历史（未启用会话 no-op）

        :param proactive: 是否主动回复产出（用于「上次主动回复距今」元信息）
        """
        state = self._states.get(session_id)
        if state is None:
            return
        try:
            # 与 buffer 消息（handler 侧）同用分钟精度，保持决策历史内时间格式一致
            text = f"[{asia_shanghai_now_str('%Y-%m-%d %H:%M')}] {_bot_name()}：{content}"
            await state.history.append("user", text)
            now = time.time()
            state.last_bot_at = now
            if proactive:
                state.last_proactive_at = now
            store = state.history.store
            await store.set_gate_state(session_id, "last_bot_at", str(now))
            if proactive:
                await store.set_gate_state(session_id, "last_proactive_at", str(now))
        except Exception as e:
            logger.opt(exception=e).warning(f"主动回复发言记录失败 {session_id}")

    # ===== 防抖到期编排 =====

    def _schedule_timer(self, session_id: str, state: SessionGateState) -> None:
        """（重新）安排防抖到期唤醒任务：observe 后调用，旧任务被取消"""
        deadline = state.debouncer.deadline()
        if deadline is None:
            return
        if state.timer is not None:
            state.timer.cancel()
        delay = max(0.0, deadline - monotonic())
        state.timer = asyncio.create_task(self._timer_fire(session_id, delay))

    async def _timer_fire(self, session_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        try:
            await self._on_debounce_due(session_id)
        except Exception as e:
            logger.opt(exception=e).warning(f"主动回复防抖到期处理失败 {session_id}")

    async def _on_debounce_due(self, session_id: str) -> None:
        """防抖到期：consume 批次（重新开批）→ 退避检查 → 概率闸 + 决策

        到期即 consume：之后一切以 buffer/日志为准，退避延迟重查、概率
        跳过、决策中到达的新消息都属于下一批，无补偿逻辑。
        """
        state = self._states.get(session_id)
        if state is None:
            return
        state.timer = None
        state.debouncer.consume()
        if not self._still_enabled(state):
            return

        auto_cfg = cfg.auto_reply
        if state.backoff_count >= auto_cfg.backoff_start:
            # 退避中：到期不立即送审，延迟 base×2^n 秒后重走概率闸
            if state.backoff_timer is None:
                delay = min(
                    auto_cfg.backoff_base * (2 ** (state.backoff_count - auto_cfg.backoff_start)),
                    auto_cfg.backoff_cap,
                )
                logger.info(f"退避延迟 {session_id} 计数={state.backoff_count} 延迟={delay:.0f}s")
                state.backoff_timer = asyncio.create_task(self._backoff_fire(session_id, delay))
            return

        await self._try_gate(session_id, state)

    async def _backoff_fire(self, session_id: str, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return
        state = self._states.get(session_id)
        if state is None:
            return
        state.backoff_timer = None
        if not self._still_enabled(state):
            return
        # 重查对着当前 buffer 全量评估：期间无新消息且概率未抽中则批次跳过，
        # 不会对同一批无限重试
        try:
            await self._try_gate(session_id, state)
        except Exception as e:
            logger.opt(exception=e).warning(f"主动回复退避重查失败 {session_id}")

    async def _try_gate(self, session_id: str, state: SessionGateState) -> None:
        """决策流水线入口（防重入）：同一会话同时最多一个决策任务"""
        if state.gate_running:
            logger.debug(f"主动回复决策进行中，本批放弃 {session_id}")
            return
        state.gate_running = True
        try:
            await self._gate_flow(session_id, state)
        finally:
            state.gate_running = False

    async def _gate_flow(self, session_id: str, state: SessionGateState) -> None:
        """概率闸 → LLM 决策 → （回）执行主动回复"""
        now = asia_shanghai_now()
        probability, window = window_probability(now)
        if random.random() >= probability:
            state.counters.skipped += 1
            logger.info(
                f"批次跳过 {session_id} 时段={describe_window(window)} "
                f"概率={probability:.2f} 未抽中，消息留 buffer"
            )
            return

        outcome = await state.history.decide(
            self._build_meta(state, now),
            timeout=cfg.auto_reply.gate_timeout,
        )
        if outcome is None:
            # 失败即放弃：不回、不计退避、不重试
            state.counters.failures += 1
            return

        decision, usage = outcome.decision, outcome.usage
        state.counters.input_tokens += usage.input_tokens
        state.counters.cache_read_tokens += usage.cache_read_tokens
        # 送审覆盖的新增消息量：含概率跳过/退避期间累积的批次（防抖批次已 consume）
        batch_size = state.new_messages
        state.new_messages = 0
        backoff_desc = (
            f"计数={state.backoff_count}"
            if state.backoff_count >= cfg.auto_reply.backoff_start
            else "无"
        )
        logger.info(
            f"主动回复决策 {session_id} 批={batch_size}条 "
            f"时段={describe_window(window)} 退避={backoff_desc} → "
            f"{'回复' if decision.reply else '沉默'} | {decision.reason} | "
            f"tokens={usage.input_tokens} cache={usage.cache_read_tokens}/{usage.input_tokens}"
        )

        if decision.reply:
            state.backoff_count = 0
            state.counters.replies += 1
            await self._execute_reply(session_id, state, reason=decision.reason)
        else:
            state.backoff_count += 1
            state.counters.silences += 1

    def _build_meta(self, state: SessionGateState, now: datetime) -> GateMeta:
        """构建本次决策轮尾部元信息"""
        now_ts = time.time()
        return GateMeta(
            new_messages=state.new_messages,
            now=now,
            since_bot_s=(now_ts - state.last_bot_at) if state.last_bot_at else None,
            since_proactive_s=(
                (now_ts - state.last_proactive_at) if state.last_proactive_at else None
            ),
            silence_streak=state.backoff_count,
        )

    async def _execute_reply(
        self, session_id: str, state: SessionGateState, *, reason: str | None = None
    ) -> None:
        """决策通过后的回复执行：流式产出经 deliver 主动发回

        :param reason: 决策器判定理由，随系统通知注入主 Agent 提示词
        """
        bot, session_info = state.bot, state.session_info
        if bot is None or session_info is None:
            logger.warning(f"主动回复执行缺少 bot 或会话信息，放弃 {session_id}")
            return

        async def _record(content: str) -> None:
            await auto_reply_runtime.on_bot_message(session_id, content, proactive=True)

        try:
            await send_auto_reply(bot, session_info, reason=reason, on_content=_record)
        except Exception as e:
            logger.opt(exception=e).warning(f"主动回复执行失败 {session_id}")

    # ===== buffer 消费回调（被动回复后重置防抖） =====

    def on_buffer_consumed(self, session_id: str) -> None:
        """被动回复消费了 buffer：取消挂起的防抖任务、重置批次

        退避计数与决策日志保留（同步轻操作，在会话锁内调用）。
        """
        state = self._states.get(session_id)
        if state is None:
            return
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        state.debouncer.consume()

    # ===== 上下文清理 =====

    async def _daily_reset(self) -> None:
        """每日定时清空：所有在线会话的决策上下文内存生效序列归零

        DB 全量决策保留不删；未加载的会话无需处理（其恢复口径由
        `restore_today` 的当日边界保证）。决策恰在进行中不受影响：
        其固化行属清空后的新上下文。
        """
        for state in list(self._states.values()):
            state.history.reset_context()
        logger.info(f"每日决策上下文清空完成（{len(self._states)} 个在线会话）")

    async def clear_history(self, session_id: str) -> int | None:
        """清空该会话的决策消息历史（内存生效序列 + DB 全量行）

        未进入流水线的会话（未启用/私聊）返回 None；成功返回删除的 DB 行数。
        时间戳元信息、统计计数、防抖/退避状态与主会话上下文均不受影响；
        决策进行中调用安全，至多残留本轮判定的两条记录。
        """
        state = self._states.get(session_id)
        if state is None:
            return None
        if state.load_task is not None:
            await state.load_task
            state.load_task = None
        return await state.history.clear()

    # ===== 统计 =====

    async def format_stats(self, session_id: str, group_label: str) -> str | None:
        """决策统计文本；会话未进入流水线（未启用/私聊）返回 None"""
        state = self._states.get(session_id)
        if state is None:
            return None
        if state.load_task is not None:
            await state.load_task
            state.load_task = None

        auto_cfg = cfg.auto_reply
        probability, window = window_probability()
        lines = [
            f"📊 决策统计（群 {group_label}）",
            (
                f"状态：{'启用' if self._still_enabled(state) else '停用'} | "
                f"时段概率 {probability:.2f}（{describe_window(window)}） | "
                f"退避：连续沉默 {state.backoff_count}/{auto_cfg.backoff_start}"
            ),
        ]

        if (deadline := state.debouncer.deadline()) is not None:
            lines.append(
                f"防抖：批次进行中，{max(0.0, deadline - monotonic()):.1f}s 后到期"
                f"（本批 {state.debouncer.batch_size} 条）"
            )
        else:
            lines.append("防抖：空闲（无进行中批次）")

        count, estimated = state.history.context_stats()
        lines.append(f"决策器上下文：{count} 条 / 估算 {_format_k(estimated)} tokens")

        counters = state.counters
        if counters.input_tokens:
            hit_rate = counters.cache_read_tokens / counters.input_tokens
            lines.append(
                f"缓存命中（累计）：{hit_rate:.1%}"
                f"（cache_read {_format_k(counters.cache_read_tokens)}"
                f" / input {_format_k(counters.input_tokens)}）"
            )
        else:
            lines.append("缓存命中（累计）：暂无决策调用")

        total = counters.replies + counters.silences + counters.skipped + counters.failures
        lines.append(
            f"决策累计：{total} 次 = 回 {counters.replies} + 不回 {counters.silences}"
            f" + 概率跳过 {counters.skipped} + 失败 {counters.failures}"
        )

        if state.last_bot_at is not None:
            lines.append(
                f"{_bot_name()} 上次发言：{format_duration(time.time() - state.last_bot_at)}前"
            )
        return "\n".join(lines)


auto_reply_runtime = AutoReplyRuntime()
"""主动回复运行时单例"""


def set_group_enabled(group_id: str, platform: PlatformType, enabled: bool) -> None:
    """运行时启用/禁用某群的主动回复，并持久化到 `chat_configs.json`

    启用群的存放位置与黑名单同源（`ChatConfig.auto_reply_enabled_groups`，
    按平台隔离）；`is_enabled` 每次现读，切换立即生效。
    """
    group_config = chat_configs.instance.get_by_platform(platform)
    if group_config is None:
        raise ValueError(f"不支持的平台：{platform}")
    groups = group_config.auto_reply_enabled_groups
    if enabled:
        groups.add(group_id)
    else:
        groups.discard(group_id)
    chat_configs.save_to_file()
