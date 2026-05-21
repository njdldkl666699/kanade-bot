from nonebot import on_command
from nonebot.permission import SUPERUSER

music_recommend = on_command(
    "音乐推荐",
    aliases={"听什么", "whattolisten", "what_to_listen"},
    priority=2,
    block=True,
)


playlist = on_command(
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


list_audios = on_command(
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
