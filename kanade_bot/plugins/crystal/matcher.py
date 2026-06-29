from nonebot import on_command
from nonebot.matcher import Matcher

from .enum import DaypartEnum

check_in = on_command(
    "签到",
    aliases={"checkin", "check_in", "打卡", "打卡啦摩托"},
    priority=2,
    block=True,
)

check_ins: dict[DaypartEnum, type[Matcher]] = {}

for daypart in DaypartEnum:
    check_ins[daypart] = on_command(
        daypart.value,
        priority=2,
        block=True,
    )

my_crystal = on_command(
    "我的水晶",
    aliases={"mycrystal", "my_crystal", "查看水晶"},
    priority=2,
    block=True,
)

list_handler_consumes = on_command(
    "查看命令水晶消耗",
    aliases={"查看命令消耗", "handler_consumes", "list_handler_consumes"},
    priority=2,
    block=True,
)

crystal_ranking = on_command(
    "水晶排行榜",
    aliases={"水晶排名", "crystal_ranking"},
    priority=2,
    block=True,
)

gacha = on_command(
    "抽卡",
    aliases={"gacha", "单抽"},
    priority=2,
    block=True,
)

gacha_10 = on_command(
    "十连抽",
    aliases={"gacha10", "十连抽卡", "十连", "10连", "10连抽"},
    priority=2,
    block=True,
)
