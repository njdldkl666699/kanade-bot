from nonebot import on_command, on_notice

from kanade_bot.utils.common import superuser_onebot_private_permission

help_command = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)


offline_notice = on_notice(
    priority=1,
    block=False,
)


execute_command = on_command(
    "execute",
    priority=2,
    permission=superuser_onebot_private_permission,
    block=True,
)
