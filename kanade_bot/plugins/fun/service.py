from nonebot import get_driver, get_plugin_config
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import ActionFailed, MessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import PrivateMessageEvent as OneBotPrivateMessageEvent
from nonebot.adapters.onebot.v11.helpers import Cooldown, CooldownIsolateLevel, autorevoke_send
from nonebot.params import CommandArg, EventPlainText

from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme

from .cache import UserDailyWaifuCache
from .config import Config
from .duanzi import (
    add_duanzi,
    duanzi_to_onebot_message,
    get_or_random_duanzi,
    list_paged_duanzi,
    parse_duanzi_args,
    remove_duanzi,
)
from .matcher import (
    add_a_duanzi,
    ciallo,
    list_duanzi,
    plus_one,
    random_duanzi,
    random_waifu,
    refresh_waifu,
    remove_a_duanzi,
    today_waifu,
)
from .waifu import get_compressed_image, query_lolicon_waifus, random_loli_waifu

cfg = get_plugin_config(Config).fun


@ciallo.handle()
async def handle_ciallo_console(bot: ConsoleBot):
    await ciallo.finish("Ciallo～(∠・ω< )⌒☆")


@ciallo.handle()
async def handle_ciallo_onebot(bot: OneBot):
    ciallo_image_path = cfg.ciallo_image_file_path
    if not ciallo_image_path.is_file():
        await ciallo.finish("Ciallo～(∠・ω< )⌒☆")

    await ciallo.finish(OneBotMessageSegmentMeme(ciallo_image_path))


group_message_cache: dict[str | int, list[str]] = {}


@plus_one.handle()
async def _(event: Event):
    # 获取触发阈值
    threshold = cfg.plus_one_threshold
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
    cached_messages = group_message_cache[group_id]

    # 获取当前信息
    message = event.get_message()
    if len(message) != 1 or not message[0].is_text():
        cached_messages.clear()
        return

    # 仅处理一段文本消息
    new_text: str | None = message[0].data.get("text")
    if not new_text:
        return

    # 比对当前消息和上一条消息
    last_text = cached_messages[-1] if cached_messages else None
    # 如果当前消息和上一条消息不同，则清空记录并添加当前消息
    if new_text != last_text:
        cached_messages.clear()
    cached_messages.append(new_text)

    # 如果当前消息和上一条消息相同，并且记录数量达到阈值，则+1消息并清空记录
    if len(cached_messages) >= threshold:
        await plus_one.send(new_text)
        cached_messages.clear()

    group_message_cache[group_id] = cached_messages


@random_duanzi.handle()
async def _(bot: Bot, arg_msg: Message = CommandArg()):
    args = parse_duanzi_args(arg_msg)
    index, chaos_face, custom_face_id_or_emoji = args

    if not (duanzi := get_or_random_duanzi(index)):
        await random_duanzi.finish()

    if isinstance(bot, OneBot):
        duanzi = await duanzi_to_onebot_message(
            bot,
            duanzi,
            node_threshold=500,
            chaos_face=chaos_face,
            custom_face_id_or_emoji=custom_face_id_or_emoji,
        )

    await random_duanzi.finish(duanzi)


@add_a_duanzi.handle()
async def _(event: ConsolePublicMessageEvent, arg_msg: Message = CommandArg()):
    duanzi = arg_msg.extract_plain_text().strip()
    if not duanzi:
        await add_a_duanzi.pause("请输入段子/史内容")

    if not add_duanzi(duanzi):
        await add_a_duanzi.finish("添加失败")

    await add_a_duanzi.finish("添加完成")


@add_a_duanzi.handle()
async def _(event: OneBotGroupMessageEvent, arg_msg: Message = CommandArg()):
    if reply := event.reply:
        if duanzi := reply.message.extract_plain_text().strip():
            add_duanzi(duanzi)
            await add_a_duanzi.finish("添加完成")

    duanzi = arg_msg.extract_plain_text().strip()
    if not duanzi:
        await add_a_duanzi.pause("请输入段子/史内容")

    if not add_duanzi(duanzi):
        await add_a_duanzi.finish("添加失败")

    await add_a_duanzi.finish("添加完成")


@add_a_duanzi.handle()
async def _(message: str = EventPlainText()):
    if not (duanzi := message.strip()):
        await add_a_duanzi.finish("发送消息中没有文本内容，请重新发送命令")

    if not add_duanzi(duanzi):
        await add_a_duanzi.finish("添加失败")

    await add_a_duanzi.finish("添加完成")


@list_duanzi.handle()
async def _(arg_msg: Message = CommandArg()):
    page = arg_msg.extract_plain_text().strip()
    if page.isdigit():
        page = int(page)
    else:
        page = 1

    duanzi_list_str = list_paged_duanzi(page)
    await list_duanzi.finish(duanzi_list_str)


@remove_a_duanzi.handle()
async def _(arg_msg: Message = CommandArg()):
    index_str = arg_msg.extract_plain_text().strip()
    if not index_str.isdigit():
        await remove_a_duanzi.finish("删除失败，请检查序号是否正确")

    index = int(index_str)
    if not remove_duanzi(index):
        await remove_a_duanzi.finish("删除失败，请检查序号是否正确")

    await remove_a_duanzi.finish("删除完成")


@today_waifu.handle()
async def _(event: ConsoleMessageEvent):
    user_id = event.get_user_id()
    p = UserDailyWaifuCache.get_path(user_id)
    if p:
        await today_waifu.finish(str(p))

    url = await random_loli_waifu()
    image = await get_compressed_image(url)
    if not image:
        await today_waifu.finish("获取图片失败，请稍后再试")

    p = UserDailyWaifuCache.set(user_id, image)
    await today_waifu.finish(str(p))


@today_waifu.handle()
async def _(event: OneBotMessageEvent):
    user_id = event.get_user_id()
    cache = UserDailyWaifuCache.get(user_id)
    if cache:
        await today_waifu.finish(MessageSegment.image(cache))

    url = await random_loli_waifu()
    image = await get_compressed_image(url)
    if not image:
        await today_waifu.finish("获取图片失败，请稍后再试")

    UserDailyWaifuCache.set(user_id, image)
    await today_waifu.finish(MessageSegment.image(image))


@refresh_waifu.handle()
async def _(event: Event):
    UserDailyWaifuCache.delete(event.get_user_id())
    await refresh_waifu.finish("今日老婆已刷新")


@random_waifu.handle()
async def _(event: ConsoleMessageEvent, arg_msg: Message = CommandArg()):
    json_str = arg_msg.extract_plain_text().strip()
    if not json_str:
        url = await random_loli_waifu()
        await random_waifu.finish(url)

    # 隐藏功能
    _, urls = await query_lolicon_waifus(json_str)
    if not urls:
        await random_waifu.finish("查询失败，请检查参数是否正确")
    await random_waifu.finish("\n\n".join(urls))


@random_waifu.handle(
    (Cooldown(10, prompt="别太压抑了。", isolate_level=CooldownIsolateLevel.GROUP),)
)
async def _(bot: OneBot, event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    json_str = arg_msg.extract_plain_text().strip()
    if not json_str:
        url = await random_loli_waifu()
        image = await get_compressed_image(url)
        if not image:
            await random_waifu.finish(f"获取图片失败，链接：{url}")
        try:
            await random_waifu.finish(MessageSegment.image(image))
        except ActionFailed:
            await random_waifu.finish(f"发送图片失败，链接：{url}")

    # 隐藏功能
    urls = await query_lolicon_waifus(json_str)
    if not urls:
        await random_waifu.finish("查询失败，请检查参数是否正确")

    # 私聊且是管理员，直接发链接
    if (
        isinstance(event, OneBotPrivateMessageEvent)
        and event.get_user_id() in get_driver().config.superusers
    ):
        await random_waifu.finish("\n\n".join(urls))

    # 发送混淆图片链接，并在30秒后撤回消息
    obscured_urls = [url.replace(".", "。").replace(":", "：") for url in urls]
    await autorevoke_send(bot, event, "\n\n".join(obscured_urls), revoke_time=30)
