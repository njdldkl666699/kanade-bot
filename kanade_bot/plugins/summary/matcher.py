from nonebot import on_command

summarize = on_command(
    "总结",
    aliases={"summarize", "summary"},
    priority=2,
    block=True,
)
