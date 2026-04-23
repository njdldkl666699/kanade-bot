from apscheduler.triggers.cron import CronTrigger
from nonebot import get_adapter, get_driver, logger, require
from nonebot.adapters.onebot.v11 import Adapter, Message

from .config import ScheduleConfig, schedules, write_schedules

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

bot = list(get_adapter(Adapter).bots.values())[0]
"""仅支持单bot，取第一个bot实例"""


def schedule_id(group_id: int, schedule_name: str) -> str:
    return f"tool_schedule_{group_id}_{schedule_name}"


def print_schedules_pretty(group_id: int) -> str | None:
    """列出指定群的定时任务列表"""
    group_schedules = schedules.root.get(group_id, {})
    if not group_schedules:
        return None

    lines = [f"群 {group_id} 的定时任务列表："]
    for name, config in group_schedules.items():
        lines.append(f"- {name}: {config.cron} -> {config.message.to_rich_text()}")

    return "\n".join(lines)


def add_schedule(group_id: int, name: str, cron: str, message: Message):
    """添加定时任务"""
    job_id = schedule_id(group_id, name)
    if scheduler.get_job(job_id):
        raise ValueError(f"定时任务 {name} 已存在")

    # 添加到调度器
    scheduler.add_job(
        send_scheduled_message,
        trigger=CronTrigger.from_crontab(cron),
        id=job_id,
        args=[group_id, message],
    )

    # 添加到配置
    group_schedules = schedules.root.setdefault(group_id, {})
    group_schedules[name] = ScheduleConfig(cron=cron, message=message)
    write_schedules()
    logger.info(f"已添加群 {group_id} 的定时任务 {name}: {cron} -> {message.to_rich_text()}")


def remove_schedule(group_id: int, name: str):
    """移除定时任务"""
    job_id = schedule_id(group_id, name)
    if not scheduler.get_job(job_id):
        raise ValueError(f"定时任务 {name} 不存在")

    # 从调度器移除
    scheduler.remove_job(job_id)

    # 从配置移除
    group_schedules = schedules.root.get(group_id, {})
    if name in group_schedules:
        del group_schedules[name]
        write_schedules()
        logger.info(f"已移除群 {group_id} 的定时任务 {name}")


async def send_scheduled_message(group_id: int, message: Message):
    try:
        await bot.send_group_msg(group_id=group_id, message=message)
    except Exception as e:
        logger.error(f"发送定时消息失败: {e}")


driver = get_driver()


@driver.on_startup
async def on_startup():
    for group_id, schedule_list in schedules.root.items():
        for name, schedule in schedule_list.items():
            job_id = schedule_id(group_id, name)
            scheduler.add_job(
                send_scheduled_message,
                trigger=CronTrigger.from_crontab(schedule.cron),
                id=job_id,
                args=[group_id, schedule.message],
            )
        logger.info(f"已加载群 {group_id} 的 {len(schedule_list)} 个定时任务")
