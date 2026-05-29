from nonebot import on_command


check_in = on_command(
    "签到",
    aliases={"checkin", "check_in"},
    priority=2,
    block=True,
)


my_crystal = on_command(
    "我的水晶",
    aliases={"mycrystal", "my_crystal", "查看水晶"},
    priority=2,
    block=True,
)
