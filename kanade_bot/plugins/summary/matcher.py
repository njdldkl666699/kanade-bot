from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

summarize = on_command(
    "总结",
    aliases={"summarize", "summary"},
    priority=2,
    block=True,
)
register_matcher(summarize, "总结")
