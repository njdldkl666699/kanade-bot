from nonebot import on_command, require
from nonebot.permission import SUPERUSER

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

music_recommend = on_command(
    "音乐推荐",
    aliases={"听什么", "whattolisten", "what_to_listen"},
    priority=2,
    block=True,
)
register_matcher(music_recommend, "音乐推荐")

playlist = on_command(
    "歌单列表",
    aliases={"歌单", "music_list", "musiclist"},
    priority=2,
    block=True,
)
register_matcher(playlist, "歌单列表")

sing_song = on_command(
    "唱一首歌",
    aliases={"sing_song", "sing", "唱一句", "唱一首"},
    priority=2,
    block=True,
)
register_matcher(sing_song, "唱一首歌")

list_audios = on_command(
    "唱歌列表",
    aliases={"singlist", "sing_list"},
    priority=2,
    block=True,
)
register_matcher(list_audios, "唱歌列表")

random_lyric = on_command(
    "随机歌词",
    aliases={"lyric", "random_lyric"},
    priority=2,
    block=True,
)
register_matcher(random_lyric, "随机歌词")

add_lyric = on_command(
    "添加歌词",
    aliases={"add_lyric"},
    priority=2,
    block=True,
)
register_matcher(add_lyric, "添加歌词")

remove_lyric = on_command(
    "删除歌词",
    aliases={"remove_lyric", "del_lyric"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_lyric, "删除歌词")
