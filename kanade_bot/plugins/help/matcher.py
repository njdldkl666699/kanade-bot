from nonebot import on_command, on_notice

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
