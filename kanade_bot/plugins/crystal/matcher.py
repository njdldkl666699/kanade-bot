from nonebot import on_command


check_in = on_command(
    "签到",
    aliases={"checkin", "check_in", "打卡", "打卡啦摩托"},
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
