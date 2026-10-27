"""定时任务命令"""

from nonebot.adapters import Event, Message
from nonebot.params import CommandArg

from kanade_bot.utils.session import extract_session_info_sync

from ..agent.schedule import scheduled_task_manager
from ..matcher import chat_task_cancel, chat_task_list


@chat_task_list.handle()
async def handle_chat_task_list(event: Event):
    """列出当前会话的Agent定时任务"""
    session_id = extract_session_info_sync(event).session_id
    tasks = scheduled_task_manager.list_by_session(session_id)
    if not tasks:
        await chat_task_list.finish("当前会话没有定时任务")

    lines = [f"📋 当前会话的定时任务（{len(tasks)}）："]
    for task in tasks:
        description = (
            task.description if len(task.description) <= 40 else task.description[:40] + "…"
        )
        creator = f"（by {task.creator_name}）" if task.creator_name else ""
        lines.append(f"- [{task.task_id}] {task.run_at_dt:%m-%d %H:%M} {description}{creator}")
    await chat_task_list.finish("\n".join(lines))


@chat_task_cancel.handle()
async def handle_chat_task_cancel(event: Event, arg_msg: Message = CommandArg()):
    """按ID取消当前会话创建的定时任务"""
    task_id = arg_msg.extract_plain_text().strip()
    if not task_id:
        await chat_task_cancel.finish("请提供要取消的任务ID，可用「任务列表」查询")

    session_id = extract_session_info_sync(event).session_id
    if await scheduled_task_manager.cancel(task_id, session_id=session_id):
        await chat_task_cancel.finish(f"已取消定时任务 {task_id}")
    await chat_task_cancel.finish(f"当前会话不存在任务 {task_id}（可用「任务列表」查询）")
