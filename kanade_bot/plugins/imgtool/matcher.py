from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

mirror = on_command(
    "镜像",
    aliases={"mirror"},
    priority=2,
    block=True,
)
register_matcher(mirror, "镜像")

rotate = on_command(
    "旋转",
    aliases={"rotate"},
    priority=2,
    block=True,
)
register_matcher(rotate, "旋转")

back = on_command(
    "倒放",
    aliases={"back"},
    priority=2,
    block=True,
)
register_matcher(back, "倒放")

speed = on_command(
    "倍速",
    aliases={"speed", "加速"},
    priority=2,
    block=True,
)
register_matcher(speed, "倍速")

mid = on_command(
    "对称",
    aliases={"mid"},
    priority=2,
    block=True,
)
register_matcher(mid, "对称")

flow = on_command(
    "流动",
    aliases={"flow"},
    priority=2,
    block=True,
)
register_matcher(flow, "流动")

fan = on_command(
    "转动",
    aliases={"fan"},
    priority=2,
    block=True,
)
register_matcher(fan, "转动")
