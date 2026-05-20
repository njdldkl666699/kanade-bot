from nonebot import on_command, on_fullmatch, on_message
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="各种娱乐功能",
    usage="",
    config=Config,
)


ciallo = on_fullmatch(
    ("Ciallo", "Ciallo～(∠・ω< )⌒☆", "Ciallo～(∠・ω< )⌒★"),
    priority=2,
    ignorecase=True,
    block=True,
)


plus_one = on_message(priority=5, block=False)


music_listen = on_command(
    "音乐推荐",
    aliases={"听什么", "whattolisten", "what_to_listen"},
    priority=2,
    block=True,
)


music_list = on_command(
    "歌单列表",
    aliases={"歌单", "music_list", "musiclist"},
    priority=2,
    block=True,
)


sing_song = on_command(
    "唱一首歌",
    aliases={"sing_song", "sing", "唱一句", "唱一首"},
    priority=2,
    block=True,
)


list_sing_song = on_command(
    "唱歌列表",
    aliases={"singlist", "sing_list"},
    priority=2,
    block=True,
)


random_lyric = on_command(
    "随机歌词",
    aliases={"lyric", "random_lyric"},
    priority=2,
    block=True,
)


add_lyric = on_command(
    "添加歌词",
    aliases={"add_lyric"},
    priority=2,
    block=True,
)


remove_lyric = on_command(
    "删除歌词",
    aliases={"remove_lyric", "del_lyric"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


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


__all__ = [
    "ciallo",
    "plus_one",
    "music_listen",
    "music_list",
    "sing_song",
    "list_sing_song",
    "random_lyric",
    "add_lyric",
    "remove_lyric",
    "random_duanzi",
    "add_a_duanzi",
    "list_duanzi",
    "remove_a_duanzi",
]
