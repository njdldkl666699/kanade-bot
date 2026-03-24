import base64
from pathlib import Path

from nonebot import get_plugin_config, on_command, on_fullmatch
from nonebot.adapters import Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from kanade_bot.plugins.fun.music import get_music_list_names, get_random_music

from .config import Config

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
    message = MessageSegment.image(Path(cfg.fun_ciallo_image_path))
    message.data["summary"] = "[动画表情]"
    message.data["sub_type"] = 1
    await ciallo.finish(message)


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
    await music_listen.finish(f"听 {list_name} 歌单的\n{music.to_pretty_string()}")


@music_listen.handle()
async def handle_music_listen_onebot(bot: OneBot, arg_msg: Message = CommandArg()):
    list_query = arg_msg.extract_plain_text().strip()
    try:
        list_name, music = get_random_music(list_query)
    except ValueError as e:
        await music_listen.finish(str(e))

    await music_listen.send(f"听 {list_name} 歌单的\n")
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
            text_message = music.to_pretty_string()
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


thunder_link_parse = on_command(
    "迅雷链接解析",
    aliases={"thunder_parse", "thunder_link_parse"},
    priority=2,
    block=True,
)


@thunder_link_parse.handle()
async def _(arg_msg: Message = CommandArg()):
    thunder_link = arg_msg.extract_plain_text().strip()
    if not thunder_link.startswith("thunder://"):
        await thunder_link_parse.finish("请输入有效的迅雷链接")

    try:
        decoded_bytes = base64.b64decode(thunder_link[10:])
        decoded_str = decoded_bytes.decode("utf-8")
        if decoded_str.startswith("AA") and decoded_str.endswith("ZZ"):
            decoded_str = decoded_str[2:-2]
        await thunder_link_parse.finish(decoded_str)
    except Exception as e:
        await thunder_link_parse.finish(f"解析失败: {e}")


pjsk_skill_multiplier = on_command(
    "技能倍率",
    aliases={"倍率"},
    priority=2,
    block=True,
)


@pjsk_skill_multiplier.handle()
async def _(arg_msg: Message = CommandArg()):
    args = arg_msg.extract_plain_text().strip().split()
    multipliers = [int(arg) for arg in args if arg.isdigit()]
    if len(multipliers) != 5:
        await pjsk_skill_multiplier.finish("请输入5个技能倍率，格式如：/倍率 100 100 100 100 100")

    captain = multipliers[0]
    members = sum(multipliers[1:]) / 5
    total_multiplier = captain + members
    await pjsk_skill_multiplier.finish(
        "您的卡组技能效果如下\n"
        f"车头: {captain}%\n"
        f"内部: {members}%\n"
        f"倍率: {total_multiplier / 100 + 1}\n"
        f"技能实际值为: {total_multiplier}%"
    )
