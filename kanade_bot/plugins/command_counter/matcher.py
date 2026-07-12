from nonebot import on_command

top_commands = on_command(
    "命令排行",
    aliases={"top_commands", "查看命令排行"},
    priority=2,
    block=True,
)
