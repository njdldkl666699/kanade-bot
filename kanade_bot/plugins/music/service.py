from io import BytesIO

from nonebot import get_plugin_config
from nonebot.adapters import Bot, Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg, EventPlainText
from nonebot.typing import T_State

from kanade_bot.utils.parse import parse_arg_message

from .audio import get_audio_pages, get_or_random_audio, query_audios, random_clip_audio
from .config import Config
from .lyric import add_lyric_txt, get_random_lyric, remove_song_lyric
from .matcher import (
    add_lyric,
    list_audios,
    music_recommend,
    playlist,
    random_lyric,
    remove_lyric,
    sing_song,
)
from .music import get_playlist_names, get_random_music

cfg = get_plugin_config(Config).music


@music_recommend.handle()
async def handle_music_listen(bot: ConsoleBot, arg_msg: Message = CommandArg()):
    list_query = arg_msg.extract_plain_text().strip()
    try:
        list_name, music = get_random_music(list_query)
    except ValueError as e:
        await music_recommend.finish(str(e))
    await music_recommend.finish(f"听 {list_name} 歌单的\n{music.pretty_string}")


@music_recommend.handle()
async def handle_music_listen_onebot(bot: OneBot, arg_msg: Message = CommandArg()):
    list_query = arg_msg.extract_plain_text().strip()
    try:
        list_name, music = get_random_music(list_query)
    except ValueError as e:
        await music_recommend.finish(str(e))

    await music_recommend.send(f"听 {list_name} 歌单的")
    message = MessageSegment("music")

    match music.source:
        case "tx":
            message.data["type"] = "qq"
            message.data["id"] = str(music.meta.song_id)
        case "wy":
            message.data["type"] = "163"
            message.data["id"] = str(music.meta.song_id)
        case "kw":
            message.data["type"] = "custom"
            message.data["url"] = f"https://kuwo.cn/play_detail/{music.meta.song_id}"
            message.data["audio"] = f"https://kuwo.cn/play_detail/{music.meta.song_id}"
            message.data["title"] = f"{music.name} - {music.singer}"
            message.data["image"] = music.meta.pic_url
        # case "kg" if qualities := music.meta.qualitys:
        #     # kg暂未找到方法，改为文本消息
        #     hash = None
        #     for quality in qualities:
        #         if quality.hash:
        #             hash = quality.hash
        #             break
        #     message.data["url"] = f"https://www.kugou.com/song/#hash={hash}"
        #     message.data["audio"] = f"https://www.kugou.com/song/#hash={hash}"
        #     message.data["type"] = "custom"
        #     message.data["title"] = f"{music.name} - {music.singer}"
        #     message.data["image"] = music.meta.pic_url
        case _:
            text_message = music.pretty_string
            await music_recommend.finish(text_message)
    await music_recommend.finish(message)


@playlist.handle()
async def _():
    await playlist.finish(
        "可用的歌单列表：\n"
        + "\n".join(get_playlist_names())
        + f"\n\n完整歌单见：{cfg.playlist_link}"
    )


@sing_song.handle()
async def _(bot: Bot, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"query": str, "number": int})
    query = args["query"]
    number = args["number"]

    song_path = get_or_random_audio(query, number)
    if not song_path:
        await sing_song.finish("没有找到符合条件的歌曲")

    if isinstance(bot, ConsoleBot):
        await sing_song.finish(f"唱了 {song_path.stem} 这首歌")

    if isinstance(bot, OneBot):
        audio = random_clip_audio(song_path)
        buffer = BytesIO()
        audio.export(buffer, format="mp3")
        message = MessageSegment.record(buffer)
        await sing_song.finish(message)


@list_audios.handle()
async def _(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"page": int, "query": str})
    page = args["page"] or 1
    query = args["query"] or None

    total_pages = get_audio_pages()
    if total_pages == 0:
        await list_audios.finish("没有可用的歌曲")

    songs = query_audios(query, page)
    if not songs:
        await list_audios.finish("没有找到符合条件的歌曲")

    start_number = 1 + (page - 1) * cfg.audio_page_size

    await list_audios.finish(
        f"符合条件的歌曲列表（{page}/{total_pages} 页）:\n"
        + "\n".join(f"{i}. {song.stem}" for i, song in enumerate(songs, start_number))
    )


@random_lyric.handle()
async def _(arg_msg: Message = CommandArg()):
    args = parse_arg_message(
        arg_msg.extract_plain_text(), {"query": str, "length": int, "show_song": bool}
    )
    query: str | None = args["query"]
    length: int | None = args["length"]
    show_song: bool = args["show_song"] or True

    result = get_random_lyric(query, length)
    if not result:
        await random_lyric.finish("没有找到符合条件的歌曲")

    lyric_file, lyric_lines = result

    if isinstance(lyric_lines, list):
        lyric_text = "\n".join(line.pretty_string for line in lyric_lines)
    elif isinstance(lyric_lines, str):
        lyric_text = lyric_lines
    else:  # 要是有模式匹配就好了
        await random_lyric.finish("歌词格式错误")

    if show_song:
        await random_lyric.finish(f"{lyric_text}\n\t——{lyric_file.stem}")
    else:
        await random_lyric.finish(lyric_text)


@add_lyric.handle()
async def _(state: T_State, event: ConsolePublicMessageEvent, arg_msg: Message = CommandArg()):
    song_name = arg_msg.extract_plain_text().strip()
    if not song_name:
        await add_lyric.finish("请输入歌曲名称")

    state["song_name"] = song_name
    await add_lyric.pause("请发送歌词")


@add_lyric.handle()
async def _(state: T_State, event: OneBotGroupMessageEvent, arg_msg: Message = CommandArg()):
    song_name = arg_msg.extract_plain_text().strip()
    if not song_name:
        await add_lyric.finish("请输入歌曲名称")

    if event.reply is None:
        state["song_name"] = song_name
        await add_lyric.pause("请发送歌词")

    lyric = event.reply.message.extract_plain_text().strip()
    if not lyric:
        state["song_name"] = song_name
        await add_lyric.pause("请发送歌词")

    try:
        add_lyric_txt(song_name, lyric)
    except ValueError as e:
        await add_lyric.finish(str(e))

    await add_lyric.finish(f"已添加歌曲 {song_name}")


@add_lyric.handle()
async def _(state: T_State, message: str = EventPlainText()):
    song_name = state.get("song_name")
    if not song_name:
        await add_lyric.finish("发生错误，请重新添加歌词")

    try:
        add_lyric_txt(song_name, message)
    except ValueError as e:
        await add_lyric.finish(str(e))

    await add_lyric.finish(f"已添加歌曲 {song_name}")


@remove_lyric.handle()
async def _(arg_msg: Message = CommandArg()):
    song_name = arg_msg.extract_plain_text().strip()
    try:
        remove_song_lyric(song_name)
    except ValueError as e:
        await remove_lyric.finish(str(e))

    await remove_lyric.finish(f"已删除歌曲 {song_name}")
