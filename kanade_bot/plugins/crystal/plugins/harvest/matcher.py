from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

harvest = on_command(
    "采集资源",
    aliases={"harvest", "采集", "挖"},
    priority=2,
    block=True,
)
register_matcher(harvest, "采集资源")

harvest_all = on_command(
    "采集全部资源",
    aliases={"harvest_all", "一键采集", "自动采集"},
    priority=2,
    block=True,
)
register_matcher(harvest_all, "采集全部资源")

harvest_category = on_command(
    "查看资源采集种类",
    aliases={"harvest_category", "查看采集种类", "采集种类列表"},
    priority=2,
    block=True,
)
register_matcher(harvest_category, "查看资源采集种类")

harvest_power = on_command(
    "我的采集体力",
    aliases={"harvest_power", "我的体力"},
    priority=2,
    block=True,
)
register_matcher(harvest_power, "我的采集体力")

resume_harvest_power = on_command(
    "恢复采集体力",
    aliases={"resume_harvest_power", "恢复体力", "回复体力"},
    priority=2,
    block=True,
)
register_matcher(resume_harvest_power, "恢复采集体力")
