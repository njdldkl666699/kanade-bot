from io import BytesIO
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, on_command, on_fullmatch, on_message
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg, EventPlainText
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

from kanade_bot.plugins.util import OneBotMessageSegmentMeme, parse_arg_message

from .config import Config
from .lyric import LyricLine, add_song_lyric_txt, get_random_lyric, remove_song_lyric
from .music import get_music_list_names, get_random_music
from .sing import (
    get_or_random_sing_song,
    get_sing_song_pages,
    list_sing_songs,
    random_clip_song,
)

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


ciallo = on_fullmatch(
    ("Ciallo", "Ciallo～(∠・ω< )⌒☆", "Ciallo～(∠・ω< )⌒★"),
    priority=2,
    ignorecase=True,
    block=True,
)


@ciallo.handle()
async def handle_ciallo_console(bot: ConsoleBot):
    await ciallo.finish("Ciallo～(∠・ω< )⌒☆")


@ciallo.handle()
async def handle_ciallo_onebot(bot: OneBot):
    ciallo_image_path = Path(cfg.fun_ciallo_image_path)
    if not ciallo_image_path.is_file():
        await ciallo.finish("Ciallo～(∠・ω< )⌒☆")

    await ciallo.finish(OneBotMessageSegmentMeme(ciallo_image_path))


plus_one = on_message(priority=5, block=False)
group_message_cache: dict[str | int, list[str]] = {}


@plus_one.handle()
async def _(event: Event):
    # 获取触发阈值
    threshold = cfg.fun_plus_one_threshold
    if threshold <= 0:
        return

    # 仅处理群聊消息
    group_id = None
    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = event.group_id
    else:
        return

    # 获取群聊记录
    if group_id not in group_message_cache:
        group_message_cache[group_id] = []
    messages = group_message_cache[group_id]

    # 获取当前信息
    message = event.get_message()
    if len(message) != 1 or not message[0].is_text():
        return

    # 仅处理一段文本消息
    new_text: str | None = message[0].data.get("text")
    if not new_text:
        return

    # 比对当前消息和上一条消息
    last_text = messages[-1] if messages else None
    # 如果当前消息和上一条消息不同，则清空记录并添加当前消息
    if new_text != last_text:
        messages.clear()
    messages.append(new_text)

    # 如果当前消息和上一条消息相同，并且记录数量达到阈值，则+1消息并清空记录
    if len(messages) >= threshold:
        await plus_one.send(new_text)
        messages.clear()

    group_message_cache[group_id] = messages


# 音乐推荐
music_listen = on_command(
    "音乐推荐",
    aliases={"听什么", "whattolisten", "what_to_listen"},
    priority=2,
    block=True,
)


@music_listen.handle()
async def handle_music_listen(bot: ConsoleBot, arg_msg: Message = CommandArg()):
    list_query = arg_msg.extract_plain_text().strip()
    try:
        list_name, music = get_random_music(list_query)
    except ValueError as e:
        await music_listen.finish(str(e))
    await music_listen.finish(f"听 {list_name} 歌单的\n{music.pretty_string}")


@music_listen.handle()
async def handle_music_listen_onebot(bot: OneBot, arg_msg: Message = CommandArg()):
    list_query = arg_msg.extract_plain_text().strip()
    try:
        list_name, music = get_random_music(list_query)
    except ValueError as e:
        await music_listen.finish(str(e))

    await music_listen.send(f"听 {list_name} 歌单的")
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
            await music_listen.finish(text_message)
    await music_listen.finish(message)


music_list = on_command(
    "歌单列表",
    aliases={"歌单", "music_list", "musiclist"},
    priority=2,
    block=True,
)


@music_list.handle()
async def handle_music_list():
    await music_list.finish(
        "可用的歌单列表：\n"
        + "\n".join(get_music_list_names())
        + f"\n\n完整歌单见：{cfg.fun_music_list_link}"
    )


# 唱歌
sing_song = on_command(
    "唱一首歌",
    aliases={"sing_song", "sing", "唱一句", "唱一首"},
    priority=2,
    block=True,
)


@sing_song.handle()
async def _(bot: Bot, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"query": str, "number": int})
    query = args["query"]
    number = args["number"]

    song_path = get_or_random_sing_song(query, number)
    if not song_path:
        await sing_song.finish("没有找到符合条件的歌曲")

    if isinstance(bot, ConsoleBot):
        await sing_song.finish(f"唱了 {song_path.stem} 这首歌")

    if isinstance(bot, OneBot):
        audio = random_clip_song(song_path)
        buffer = BytesIO()
        audio.export(buffer, format="mp3")
        message = MessageSegment.record(buffer)
        await sing_song.finish(message)


list_sing_song = on_command(
    "唱歌列表",
    aliases={"singlist", "sing_list"},
    priority=2,
    block=True,
)


@list_sing_song.handle()
async def _(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"page": int, "query": str})
    page = args["page"] or 1
    query = args["query"] or None

    total_pages = get_sing_song_pages()
    if total_pages == 0:
        await list_sing_song.finish("没有可用的歌曲")

    songs = list_sing_songs(query, page)
    if not songs:
        await list_sing_song.finish("没有找到符合条件的歌曲")

    start_number = 1 + (page - 1) * cfg.fun_sing_page_size

    await list_sing_song.finish(
        f"符合条件的歌曲列表（{page}/{total_pages} 页）:\n"
        + "\n".join(f"{i}. {song.stem}" for i, song in enumerate(songs, start_number))
    )


# 歌词
random_lyric = on_command(
    "随机歌词",
    aliases={"lyric", "random_lyric"},
    priority=2,
    block=True,
)


@random_lyric.handle()
async def _(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"query": str, "length": int, "show_song": bool})
    query: str | None = args["query"]
    length: int | None = args["length"]
    show_song: bool = args["show_song"] or True
    logger.info(f"查询歌词，参数：query={query}, length={length}, show_song={show_song}")

    result = get_random_lyric(query, length)
    if not result:
        await random_lyric.finish("没有找到符合条件的歌曲")

    song_name, lyric_lines = result
    lyric_text = "\n".join(
        line.pretty_string if isinstance(line, LyricLine) else line for line in lyric_lines
    )

    if show_song:
        await random_lyric.finish(f"{lyric_text}\n\t——{song_name}")
    else:
        await random_lyric.finish(lyric_text)


add_lyric = on_command(
    "添加歌词",
    aliases={"add_lyric"},
    priority=2,
    block=True,
)


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

    add_song_lyric_txt(song_name, lyric)
    await add_lyric.finish(f"已添加歌曲 {song_name}")


@add_lyric.handle()
async def _(state: T_State, message: str = EventPlainText()):
    song_name = state.get("song_name")
    if not song_name:
        await add_lyric.finish("发生错误，请重新添加歌词")

    try:
        add_song_lyric_txt(song_name, message)
    except ValueError as e:
        await add_lyric.finish(str(e))

    await add_lyric.finish(f"已添加歌曲 {song_name}")


global_config = get_driver().config

remove_lyric = on_command(
    "删除歌词",
    aliases={"remove_lyric", "del_lyric"},
    priority=2,
    block=True,
)


@remove_lyric.handle()
async def _(event: OneBotGroupMessageEvent, arg_msg: Message = CommandArg()):
    admin_id = event.get_user_id()
    if admin_id not in global_config.superusers:
        await remove_lyric.finish()

    song_name = arg_msg.extract_plain_text().strip()
    try:
        remove_song_lyric(song_name)
    except ValueError as e:
        await remove_lyric.finish(str(e))

    await remove_lyric.finish(f"已删除歌曲 {song_name}")
