from nonebot import on_command, on_message, require
from nonebot.adapters.console.event import PrivateMessageEvent
from nonebot.adapters.onebot.v11.permission import PRIVATE
from nonebot.params import EventToMe
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

require("command_counter")
from kanade_bot.plugins.command_counter import register_matcher

chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


def not_to_me(to_me: bool = EventToMe()):
    """匹配与机器人无关的消息"""
    return not to_me


# 用于监听非@消息并将其添加到会话缓冲区，以便在下一次@消息时一起发送给模型
chat_monitor = on_message(
    rule=not_to_me,
    priority=3,
    block=False,
)

### 会话管理


def console_private_permission(event: PrivateMessageEvent) -> bool:
    """匹配Console私聊消息"""
    return True


CHAT_ADMIN = SUPERUSER | PRIVATE | console_private_permission

chat_reset = on_command(
    "重置会话",
    aliases={"会话重置", "chat_reset", "chatreset"},
    priority=2,
    permission=CHAT_ADMIN,
    block=True,
)
register_matcher(chat_reset, "重置会话")

chat_interrupt = on_command(
    "中断会话",
    aliases={"会话中断", "chat_interrupt", "chatinterrupt"},
    priority=2,
    permission=CHAT_ADMIN,
    block=True,
)
register_matcher(chat_interrupt, "中断会话")

chat_compact = on_command(
    "压缩会话",
    aliases={"会话压缩", "chat_compact", "chatcompact"},
    priority=2,
    permission=CHAT_ADMIN,
    block=True,
)
register_matcher(chat_compact, "压缩会话")

chat_workspace_clear = on_command(
    "清理工作区",
    aliases={"清除工作区", "workspace_clear", "workspaceclear"},
    priority=2,
    permission=CHAT_ADMIN,
    block=True,
)
register_matcher(chat_workspace_clear, "清理工作区")

chat_stats = on_command(
    "会话统计",
    aliases={"chat_stats", "chatstats"},
    priority=2,
    block=True,
)
register_matcher(chat_stats, "会话统计")

### 定时任务

chat_task_list = on_command(
    "任务列表",
    aliases={"chat_tasks", "chattasks"},
    priority=2,
    block=True,
)
register_matcher(chat_task_list, "任务列表")

chat_task_cancel = on_command(
    "取消任务",
    aliases={"chat_task_cancel", "chattaskcancel"},
    priority=2,
    block=True,
)
register_matcher(chat_task_cancel, "取消任务")

### 黑名单

chat_ban = on_command(
    "聊天拉黑",
    aliases={"chat_ban", "chatban"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(chat_ban, "聊天拉黑")

chat_unban = on_command(
    "聊天解除拉黑",
    aliases={"chat_unban", "chatunban"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(chat_unban, "聊天解除拉黑")

### 表情包

list_memes = on_command(
    "表情包列表",
    aliases={"list_memes", "listmemes"},
    priority=2,
    block=True,
)
register_matcher(list_memes, "表情包列表")

add_meme = on_command(
    "添加表情",
    aliases={"add_meme", "addmeme"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(add_meme, "添加表情")

retag_meme = on_command(
    "改标",
    aliases={"retag_meme", "retagmeme"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(retag_meme, "改标")

del_meme = on_command(
    "删除表情",
    aliases={"del_meme", "delmeme"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(del_meme, "删除表情")

scan_memes = on_command(
    "扫描表情",
    aliases={"scan_memes", "scanmemes"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(scan_memes, "扫描表情")
