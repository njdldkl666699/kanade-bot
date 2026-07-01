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

harvest = on_command(
    "采集资源",
    aliases={"harvest", "采集", "挖"},
    priority=2,
    block=True,
)

harvest_all = on_command(
    "采集全部资源",
    aliases={"harvest_all", "一键采集", "自动采集"},
    priority=2,
    block=True,
)

harvest_category = on_command(
    "查看资源采集种类",
    aliases={"harvest_category", "查看采集种类", "采集种类列表"},
    priority=2,
    block=True,
)

harvest_power = on_command(
    "我的采集体力",
    aliases={"harvest_power", "我的体力"},
    priority=2,
    block=True,
)

resume_harvest_power = on_command(
    "恢复采集体力",
    aliases={"resume_harvest_power", "恢复体力"},
    priority=2,
    block=True,
)
