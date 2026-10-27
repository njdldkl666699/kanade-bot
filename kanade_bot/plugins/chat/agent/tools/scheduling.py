"""调度工具：设定一次性定时任务"""

from nonebot import logger
from pydantic_ai import RunContext

from ..deps import ChatDeps
from ..schedule import parse_run_at, scheduled_task_manager


async def schedule_task(
    ctx: RunContext[ChatDeps],
    description: str,
    run_at: str = "",
    delay_minutes: float = 0,
) -> str:
    """设定一个一次性定时任务，到点后系统会唤醒你处理。

    适合用户要求未来某时刻提醒（如"半小时后提醒我""明天早上8点叫我"），
    或到点开始/汇报一项工作的场景。到点后你会收到包含任务描述的系统通知，
    届时应主动向会话反馈处理结果（如提醒对应创建者、汇报任务进展）。

    Args:
        description: 任务内容的自然语言描述，到点后会原样作为通知发给你
        run_at: 触发时间，ISO 8601格式（如 2026-10-08T08:00）；留空时用delay_minutes
        delay_minutes: 距现在的延迟分钟数（可为小数，如0.5表示30秒后）；run_at留空时必须大于0
    """
    logger.info(
        "设定定时任务工具被调用：{}，run_at={}，delay_minutes={}",
        description,
        run_at,
        delay_minutes,
    )
    try:
        fire_at = parse_run_at(run_at, delay_minutes)
    except ValueError as e:
        return f"设定定时任务失败：{e}"

    task = await scheduled_task_manager.create(
        session_info=ctx.deps.session_info,
        bot_id=ctx.deps.bot_id,
        description=description.strip(),
        run_at=fire_at,
    )
    return (
        f"定时任务已设定（ID: {task.task_id}），将于 {fire_at:%Y-%m-%d %H:%M} 触发，"
        "到点后系统会通知你处理。可以把ID告诉用户，供其用「任务列表」「取消任务」命令查询或取消。"
    )
