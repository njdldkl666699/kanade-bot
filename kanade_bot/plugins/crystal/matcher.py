from nonebot import on_command, require
from nonebot.matcher import Matcher

from .enum import DaypartEnum

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

check_in = on_command(
    "签到",
    aliases={"checkin", "check_in", "打卡", "打卡啦摩托"},
    priority=2,
    block=True,
)
register_matcher(check_in, "签到")

check_ins: dict[DaypartEnum, type[Matcher]] = {}

for daypart in DaypartEnum:
    matcher = on_command(
        daypart.value,
        priority=2,
        block=True,
    )
    check_ins[daypart] = matcher
    register_matcher(matcher, f"{daypart.value}")

my_crystal = on_command(
    "我的水晶",
    aliases={"mycrystal", "my_crystal", "查看水晶"},
    priority=2,
    block=True,
)
register_matcher(my_crystal, "我的水晶")

list_handler_consumes = on_command(
    "查看命令水晶消耗",
    aliases={"查看命令消耗", "handler_consumes", "list_handler_consumes"},
    priority=2,
    block=True,
)
register_matcher(list_handler_consumes, "查看命令水晶消耗")

crystal_ranking = on_command(
    "水晶排行榜",
    aliases={"水晶排名", "crystal_ranking"},
    priority=2,
    block=True,
)
register_matcher(crystal_ranking, "水晶排行榜")
