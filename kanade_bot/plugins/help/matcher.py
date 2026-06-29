from nonebot import on_command, on_notice
from nonebot.adapters.onebot.v11 import PRIVATE
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

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
    aliases={"exec"},
    priority=2,
    permission=SUPERUSER & PRIVATE & to_me(),
    block=True,
)


welcome = on_notice(
    priority=1,
    block=False,
)
