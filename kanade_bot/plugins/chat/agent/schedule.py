"""聊天Agent定时任务

Agent通过 `schedule_task` 工具以自然语言描述创建一次性定时任务；到点后由
APScheduler 回调主动唤醒会话：以显式 system_notification 注入 + 空用户消息
运行一轮，并把agent的回复主动发送回原会话（群或私聊）。

任务定义持久化为JSON（含会话快照与bot_id），重启后恢复，已过期的任务跳过
并清理。触发失败按配置有限重试，重试耗尽后丢弃。触发前预检创建者水晶余额，
已为负则不唤醒agent，直接发文本告知用户。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import uuid
from contextlib import aclosing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from nonebot import get_bot, get_driver, logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBot
from pydantic import BaseModel
from pydantic_ai.usage import RunUsage

from kanade_bot.utils.billing import compute_token_cost, is_peak_hours
from kanade_bot.utils.session import SessionInfo

from ..config import cfg

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler

require("crystal")
from kanade_bot.plugins.crystal import consume_crystal, get_crystal

TZ = ZoneInfo("Asia/Shanghai")
"""定时任务统一时区"""

JOB_PREFIX = "chat_scheduled_task"
"""APScheduler任务ID前缀"""

PROACTIVE_RUN_TIMEOUT = 600
"""主动唤醒运行的流事件间隔超时（秒），与用户消息驱动的一致"""


def _now() -> datetime:
    return datetime.now(TZ)


def _job_id(task_id: str) -> str:
    return f"{JOB_PREFIX}_{task_id}"


def parse_run_at(run_at: str, delay_minutes: float, *, now: datetime | None = None) -> datetime:
    """把工具参数解析为触发时间（上海时区aware datetime）

    `run_at`（ISO 8601）与 `delay_minutes` 必须恰好提供一个；
    非法输入抛 `ValueError`，消息可直接反馈给模型纠正。
    """
    now = now or _now()
    if run_at.strip() and delay_minutes > 0:
        raise ValueError(
            "run_at 与 delay_minutes 只能提供一个：指定具体时刻用 run_at，指定多久之后用 delay_minutes"
        )
    if not run_at.strip():
        if delay_minutes <= 0:
            raise ValueError(
                "必须提供 run_at（ISO 8601具体时刻）或大于0的 delay_minutes（延迟分钟数）"
            )
        return now + timedelta(minutes=delay_minutes)

    text = run_at.strip()
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as e:
        raise ValueError(
            f"无法解析时间 {text!r}，请使用ISO 8601格式，如 2026-10-08T08:00 或 2026-10-08 08:00:30"
        ) from e
    dt = dt.replace(tzinfo=TZ) if dt.tzinfo is None else dt.astimezone(TZ)
    if dt <= now:
        raise ValueError(f"触发时间必须晚于当前时间（现在是 {now:%Y-%m-%d %H:%M:%S}）")
    return dt


class ScheduledTask(BaseModel):
    """一次性定时任务"""

    task_id: str
    """任务ID（6位十六进制，供用户命令查询/取消）"""

    session_id: str
    """目标会话ID"""

    session_info: SessionInfo
    """创建时的会话信息快照，触发时据此定位发送目标与计费作用域"""

    bot_id: str | None = None
    """创建时的OneBot Bot实例ID"""

    description: str
    """任务内容的自然语言描述，到点后原样作为系统通知"""

    creator_id: str | None = None
    """创建者用户ID"""

    creator_name: str | None = None
    """创建者昵称"""

    created_at: str
    """创建时间（ISO 8601）"""

    run_at: str
    """下次触发时间（ISO 8601）；重试时会更新为重试时间"""

    attempts: int = 0
    """已执行的尝试次数（含首次）"""

    @property
    def run_at_dt(self) -> datetime:
        return datetime.fromisoformat(self.run_at)


class ScheduledTaskStore:
    """定时任务JSON文件持久化"""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> list[ScheduledTask]:
        if not self.path.is_file():
            return []
        try:
            tasks = [ScheduledTask.model_validate(item) for item in _load_json(self.path)]
        except Exception as e:
            logger.exception(f"加载定时任务文件失败，视为无任务: {e}")
            return []
        return tasks

    def save(self, tasks: list[ScheduledTask]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _save_json_atomic(self.path, [t.model_dump(mode="json") for t in tasks])


def _load_json(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save_json_atomic(path: Path, data: list[dict]) -> None:
    """先写临时文件再原子替换，避免写入中途被杀导致文件损坏"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ScheduledTaskManager:
    """定时任务生命周期管理：创建、取消、持久化与到点触发"""

    def __init__(
        self,
        store: ScheduledTaskStore,
        *,
        retry_limit: int,
        retry_delay_minutes: int,
    ):
        self._store = store
        self._retry_limit = retry_limit
        self._retry_delay = timedelta(minutes=retry_delay_minutes)
        self._tasks: dict[str, ScheduledTask] = {}
        self._lock = asyncio.Lock()

    # ===== 生命周期 =====

    async def startup(self) -> None:
        """加载持久化任务并注册调度，跳过已过期任务"""
        tasks = await asyncio.to_thread(self._store.load)
        now = _now()
        alive = [t for t in tasks if t.run_at_dt > now]
        expired = len(tasks) - len(alive)
        for task in tasks:
            if task.run_at_dt <= now:
                logger.warning(
                    f"定时任务{task.task_id}（会话{task.session_id}）触发时间已过，跳过：{task.description}"
                )
        async with self._lock:
            self._tasks = {t.task_id: t for t in alive}
            await asyncio.to_thread(self._store.save, alive)
        for task in alive:
            self._schedule_job(task)
        if tasks:
            logger.info(f"已恢复{len(alive)}个定时任务，跳过{expired}个过期任务")

    def _schedule_job(self, task: ScheduledTask) -> None:
        scheduler.add_job(
            self._fire,
            "date",
            run_date=task.run_at_dt,
            id=_job_id(task.task_id),
            args=[task.task_id],
            replace_existing=True,
            misfire_grace_time=None,
        )

    @staticmethod
    def _remove_job(task_id: str) -> None:
        try:
            scheduler.remove_job(_job_id(task_id))
        except Exception as e:
            # job已不存在（已触发完成的date job会被自动移除）
            logger.debug(f"移除定时任务job时失败（通常为job已不存在）: {e}")

    # ===== 对外操作 =====

    async def create(
        self,
        *,
        session_info: SessionInfo,
        bot_id: str | None,
        description: str,
        run_at: datetime,
    ) -> ScheduledTask:
        """创建定时任务并持久化、注册调度"""
        async with self._lock:
            task_id = uuid.uuid4().hex[:6]
            while task_id in self._tasks:
                task_id = uuid.uuid4().hex[:6]
            task = ScheduledTask(
                task_id=task_id,
                session_id=session_info.session_id,
                session_info=session_info,
                bot_id=bot_id,
                description=description,
                creator_id=session_info.user_id,
                creator_name=session_info.nickname,
                created_at=_now().isoformat(),
                run_at=run_at.isoformat(),
            )
            self._tasks[task_id] = task
            await asyncio.to_thread(self._store.save, list(self._tasks.values()))
        self._schedule_job(task)
        logger.info(
            f"会话{task.session_id}创建定时任务{task.task_id}，"
            f"将于{run_at:%Y-%m-%d %H:%M:%S}触发：{description}"
        )
        return task

    async def cancel(self, task_id: str, *, session_id: str | None = None) -> bool:
        """取消定时任务；指定session_id时仅当任务属于该会话才允许取消"""
        async with self._lock:
            task = self._tasks.get(task_id)
            if task is None or (session_id is not None and task.session_id != session_id):
                return False
            del self._tasks[task_id]
            await asyncio.to_thread(self._store.save, list(self._tasks.values()))
        self._remove_job(task_id)
        logger.info(f"定时任务{task_id}已取消：{task.description}")
        return True

    def list_by_session(self, session_id: str) -> list[ScheduledTask]:
        """列出会话的全部待触发任务，按触发时间升序"""
        return sorted(
            (t for t in self._tasks.values() if t.session_id == session_id),
            key=lambda t: t.run_at_dt,
        )

    # ===== 触发 =====

    async def _fire(self, task_id: str) -> None:
        """APScheduler回调：执行任务，失败按配置重试，耗尽后丢弃"""
        async with self._lock:
            task = self._tasks.get(task_id)
        if task is None:
            return

        try:
            await self._execute(task)
        except Exception as e:
            task.attempts += 1
            if task.attempts <= self._retry_limit:
                retry_at = _now() + self._retry_delay
                task.run_at = retry_at.isoformat()
                async with self._lock:
                    await asyncio.to_thread(self._store.save, list(self._tasks.values()))
                self._schedule_job(task)
                logger.warning(
                    f"定时任务{task_id}第{task.attempts}次触发失败：{e}，"
                    f"将于{retry_at:%Y-%m-%d %H:%M:%S}重试"
                )
                return
            logger.error(f"定时任务{task_id}重试耗尽（共尝试{task.attempts}次），已放弃：{e}")

        # 成功（或重试耗尽放弃）：移除任务
        async with self._lock:
            self._tasks.pop(task_id, None)
            await asyncio.to_thread(self._store.save, list(self._tasks.values()))
        self._remove_job(task_id)

    async def _execute(self, task: ScheduledTask) -> None:
        """唤醒会话处理任务并把回复主动发回原会话

        任何异常向上抛出，由 `_fire` 决定重试；正常返回视为任务完成。
        """
        # 延迟导入避免循环依赖（manager → tool → schedule → manager）
        from ..ban import is_banned
        from ..deliver import send_onebot_proactive, send_text_onebot_proactive
        from .manager import chat_manager

        info = task.session_info

        # 目标会话已被拉黑：静默放弃（视为完成，不重试）
        if info.platform:
            if info.group_id and is_banned(info.group_id, "group", info.platform):
                logger.info(f"定时任务{task.task_id}的目标群{info.group_id}已拉黑，放弃执行")
                return
            if info.user_id and is_banned(info.user_id, "user", info.platform):
                logger.info(f"定时任务{task.task_id}的目标用户{info.user_id}已拉黑，放弃执行")
                return

        try:
            bot = get_bot(task.bot_id)
        except (KeyError, ValueError) as e:
            raise RuntimeError(f"Bot {task.bot_id} 不在线") from e
        if not isinstance(bot, OneBot):
            raise TypeError(f"会话{task.session_id}所在平台的Bot类型不支持主动发送定时任务结果")

        # 计费预检：创建者余额已为负则不唤醒agent，直接文本告知
        platform = info.platform
        creator = task.creator_id
        if platform and creator:
            crystal = get_crystal(platform, creator)
            if crystal < 0:
                await send_text_onebot_proactive(
                    bot,
                    info,
                    f"定时任务到点：{task.description}\n"
                    f"但创建者（{task.creator_name or creator}）水晶余额不足（当前 {crystal}），已跳过执行。",
                )
                logger.warning(f"定时任务{task.task_id}因创建者余额为负跳过执行")
                return

        notification = "\n".join(
            [
                "你设定的定时任务已到点，请处理并主动向会话反馈结果。",
                f"任务内容：{task.description}",
                f"创建者：{task.creator_name or task.creator_id or '未知'}",
                f"设定时间：{task.created_at}",
            ]
        )

        turn_start = _now()

        def _bill(usage: RunUsage, produced: bool) -> None:
            """主动唤醒的运行按实际usage扣创建者水晶，峰谷按触发时刻判定"""
            if not produced or not platform or not creator:
                return
            cost = compute_token_cost(usage, cfg.billing, peak=is_peak_hours(turn_start))
            consume_crystal(platform, creator, cost)

        async with aclosing(
            chat_manager.send_and_wait(
                info,
                "",
                bot_id=task.bot_id,
                timeout=PROACTIVE_RUN_TIMEOUT,
                on_usage=_bill,
                system_notification=notification,
            )
        ) as contents:
            async for content in contents:
                if content := content.strip():
                    await send_onebot_proactive(bot, info, content)


scheduled_task_manager = ScheduledTaskManager(
    ScheduledTaskStore(cfg.scheduled_task.data_file_path),
    retry_limit=cfg.scheduled_task.retry_limit,
    retry_delay_minutes=cfg.scheduled_task.retry_delay_minutes,
)
"""聊天Agent定时任务管理器单例"""

driver = get_driver()
driver.on_startup(scheduled_task_manager.startup)
