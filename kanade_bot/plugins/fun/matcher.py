from nonebot import on_command, on_fullmatch, on_message
from nonebot.permission import SUPERUSER

ciallo = on_fullmatch(
    ("Ciallo", "Ciallo～(∠・ω< )⌒☆", "Ciallo～(∠・ω< )⌒★"),
    priority=2,
    ignorecase=True,
    block=True,
)


plus_one = on_message(priority=5, block=False)


random_duanzi = on_command(
    "随机段子",
    aliases={"duanzi", "段子", "史", "发史", "随机史"},
    priority=2,
    block=True,
)


add_a_duanzi = on_command(
    "添加段子",
    aliases={"add_duanzi", "添史"},
    priority=2,
    block=True,
)


list_duanzi = on_command(
    "段子列表",
    aliases={"list_duanzi", "史官"},
    priority=2,
    block=True,
)


remove_a_duanzi = on_command(
    "删除段子",
    aliases={"remove_duanzi", "del_duanzi", "删史"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


today_waifu = on_command(
    "今日老婆",
    aliases={"今天老婆", "today_waifu"},
    priority=2,
    block=True,
)


refresh_waifu = on_command(
    "刷新老婆",
    aliases={"刷新今日老婆", "刷新今天老婆", "refresh_waifu"},
    priority=2,
    block=True,
)


random_waifu = on_command(
    "随机图",
    aliases={"随机二次元图", "random_waifu"},
    priority=2,
    block=True,
)
