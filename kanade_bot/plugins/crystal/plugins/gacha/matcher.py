from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

gacha = on_command(
    "抽卡",
    aliases={"gacha", "单抽"},
    priority=2,
    block=True,
)
register_matcher(gacha, "抽卡")

gacha_10 = on_command(
    "十连抽",
    aliases={"gacha10", "十连抽卡", "十连", "10连", "10连抽"},
    priority=2,
    block=True,
)
register_matcher(gacha_10, "十连抽")
