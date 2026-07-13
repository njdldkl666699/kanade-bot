from nonebot import on_command, on_notice, require

from kanade_bot.utils.common import superuser_onebot_private_permission

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

help_command = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)
register_matcher(help_command, "帮助")

offline_notice = on_notice(
    priority=1,
    block=False,
)

execute_command = on_command(
    "execute",
    aliases={"exec"},
    priority=2,
    permission=superuser_onebot_private_permission,
    block=True,
)
register_matcher(execute_command, "execute")

welcome = on_notice(
    priority=1,
    block=False,
)
