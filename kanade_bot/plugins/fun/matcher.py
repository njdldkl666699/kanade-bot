from nonebot import on_command, on_fullmatch, on_message, require
from nonebot.permission import SUPERUSER

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

ciallo = on_fullmatch(
    ("Ciallo", "Ciallo～(∠・ω< )⌒☆", "Ciallo～(∠・ω< )⌒★"),
    priority=2,
    ignorecase=True,
    block=True,
)
register_matcher(ciallo, "Ciallo")

plus_one = on_message(priority=5, block=False)

random_duanzi = on_command(
    "随机段子",
    aliases={"duanzi", "段子", "史", "发史", "随机史"},
    priority=2,
    block=True,
)
register_matcher(random_duanzi, "随机段子")

add_a_duanzi = on_command(
    "添加段子",
    aliases={"add_duanzi", "添史"},
    priority=2,
    block=True,
)
register_matcher(add_a_duanzi, "添加段子")

list_duanzi = on_command(
    "段子列表",
    aliases={"list_duanzi", "史官"},
    priority=2,
    block=True,
)
register_matcher(list_duanzi, "段子列表")

remove_a_duanzi = on_command(
    "删除段子",
    aliases={"remove_duanzi", "del_duanzi", "删史"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_a_duanzi, "删除段子")

today_waifu = on_command(
    "今日老婆",
    aliases={"今天老婆", "today_waifu"},
    priority=2,
    block=True,
)
register_matcher(today_waifu, "今日老婆")

refresh_waifu = on_command(
    "刷新老婆",
    aliases={"刷新今日老婆", "刷新今天老婆", "refresh_waifu"},
    priority=2,
    block=True,
)
register_matcher(refresh_waifu, "刷新老婆")

random_waifu = on_command(
    "随机图",
    aliases={"随机二次元图", "random_waifu"},
    priority=2,
    block=True,
)
register_matcher(random_waifu, "随机图")
