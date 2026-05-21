from nonebot import on_command, on_message
from nonebot.adapters.onebot.v11 import PRIVATE
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

from kanade_bot.utils.common import console_private_permission, not_to_me

chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


# 用于监听非@消息并将其添加到会话缓冲区，以便在下一次@消息时一起发送给模型
chat_monitor = on_message(
    rule=not_to_me,
    priority=3,
    block=False,
)


chat_reset = on_command(
    "重置会话",
    aliases={"chat_reset", "chatreset", "重置对话"},
    priority=2,
    permission=SUPERUSER | PRIVATE | console_private_permission,
    block=True,
)


chat_ban = on_command(
    "聊天拉黑",
    aliases={"chat_ban", "chatban"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


chat_unban = on_command(
    "聊天解除拉黑",
    aliases={"chat_unban", "chatunban"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


list_memes = on_command(
    "表情包列表",
    aliases={"list_memes", "listmemes"},
    priority=2,
    block=True,
)


add_meme = on_command(
    "添加表情",
    aliases={"add_meme", "addmeme"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
