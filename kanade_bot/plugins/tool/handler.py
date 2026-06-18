import base64
import random

import emoji
from mcstatus import JavaServer
from nonebot import logger, require
from nonebot.adapters import Event, Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment, PokeNotifyEvent
from nonebot.exception import ActionFailed
from nonebot.params import CommandArg, EventMessage
from nonebot.typing import T_State

from kanade_bot.utils.onebot11 import send_poke, set_msg_emoji_like
from kanade_bot.utils.parse import parse_arg_message

from .config import preset_reaction_cfg_ptr
from .matcher import (
    add_a_schedule,
    list_schedules,
    mc_status,
    pjsk_skill_multiplier,
    receive_poke,
    remove_a_schedule,
    send_a_poke,
    send_emoji_like,
    send_face,
    send_like,
    thunder_link_parse,
)
from .mcstatus import render_mc_status
from .schedule import add_schedule, print_schedules_pretty, remove_schedule

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file


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


@pjsk_skill_multiplier.handle()
async def _(bot: ConsoleBot, arg_msg: Message = CommandArg()):
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


@mc_status.handle()
async def _(event: Event, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"address": str, "theme": str})
    address: str | None = args["address"]
    if address is None:
        await mc_status.finish("请提供服务器地址")

    theme: str | None = args["theme"]
    if theme not in ("light", "dark"):
        theme = "light"

    try:
        # 使用 JavaServer.async_lookup 以支持 SRV 记录解析。
        server = await JavaServer.async_lookup(address)
        status = await server.async_status()
    except Exception as e:
        logger.warning(f"查询服务器状态失败: {e}")
        await mc_status.finish("服务器查询失败")

    # 展示实际连接端口（SRV 解析后可能与输入不同）。
    image = await render_mc_status(status, address, theme)
    if isinstance(event, OneBotMessageEvent):
        # 发送图片消息
        await mc_status.finish(MessageSegment.image(image))

    # 其他平台保存图片文件
    image_path = get_plugin_cache_file("mc_status.png")
    image_path.write_bytes(image)
    await mc_status.finish("服务器状态已保存到 mc_status.png")


@list_schedules.handle()
async def _(event: OneBotGroupMessageEvent):
    group_id = event.group_id
    pretty_list = print_schedules_pretty(group_id) or "当前没有定时任务"
    await list_schedules.finish(pretty_list)


@add_a_schedule.handle()
async def _(state: T_State, event: OneBotGroupMessageEvent, arg_msg: Message = CommandArg()):
    group_id = event.group_id
    args = parse_arg_message(arg_msg.extract_plain_text(), {"name": str, "cron": str}, maxsplit=1)
    name: str | None = args["name"]
    cron: str | None = args["cron"]
    if not all([name, cron]):
        await add_a_schedule.finish("请重新提供定时任务名称、Cron表达式")

    state["group_id"] = group_id
    state["name"] = name
    state["cron"] = cron
    await add_a_schedule.pause("请发送定时任务消息内容：")


@add_a_schedule.handle()
async def _(state: T_State, bot: OneBot, message: OneBotMessage = EventMessage()):
    try:
        add_schedule(bot, state["group_id"], state["name"], state["cron"], message)
    except ValueError as e:
        await add_a_schedule.finish(str(e))
    await add_a_schedule.finish(f"已添加定时任务 {state['name']}")


@remove_a_schedule.handle()
async def _(event: OneBotGroupMessageEvent, arg_msg: Message = CommandArg()):
    group_id = event.group_id
    name = arg_msg.extract_plain_text().strip()
    if not name:
        await remove_a_schedule.finish("请提供定时任务名称")

    try:
        remove_schedule(group_id, name)
    except ValueError as e:
        await remove_a_schedule.finish(str(e))
    await remove_a_schedule.finish(f"已移除定时任务 {name}")


@send_emoji_like.handle()
async def _(bot: OneBot, event: OneBotMessageEvent, arg_msg: OneBotMessage = CommandArg()):
    message_id = reply.message_id if (reply := event.reply) else event.message_id
    emoji_id: int | None = None

    for segment in arg_msg:
        if segment.type == "face":
            emoji_id = segment.data["id"]
        if segment.type == "text":
            text: str = segment.data["text"].strip()
            if text.isnumeric():
                emoji_id = int(text)
            if emoji.is_emoji(text):
                # 部分emoji可能包含变体选择器（如 \ufe0f），需要去除后再转换为码点ID
                emoji_stripped = text.replace("\ufe0f", "").replace("\ufe0e", "")
                emoji_id = int.from_bytes(emoji_stripped.encode("utf-32-be"), "big")

    if emoji_id is None:
        await send_emoji_like.finish(
            "请提供一个表情或单个emoji字符（部分emoji可能为多个码位组成，无法使用）"
        )

    try:
        await set_msg_emoji_like(bot, message_id, emoji_id)
    except ActionFailed:
        await send_emoji_like.finish("设置表情点赞失败，可能是表情ID无效")


@send_a_poke.handle()
async def _(bot: OneBot, event: OneBotMessageEvent, message: OneBotMessage = CommandArg()):
    user_id: str | int = event.user_id
    group_id: int | None = None

    for segment in message:
        if segment.type == "at":
            user_id = segment.data["qq"]

    if isinstance(event, OneBotGroupMessageEvent):
        group_id = event.group_id

    await send_poke(bot, user_id, group_id)
    await send_a_poke.finish()


@receive_poke.handle()
async def _(bot: OneBot, event: PokeNotifyEvent):
    i = random.randint(1, 100)
    cfg = preset_reaction_cfg_ptr.v
    if i < cfg.send_poke_probability:
        # 戳回去
        await send_poke(bot, event.user_id, event.group_id)
    await receive_poke.finish(random.choice(cfg.receive_poke_messages))


@send_like.handle()
async def _(bot: OneBot, event: OneBotMessageEvent):
    cfg = preset_reaction_cfg_ptr.v
    try:
        await bot.send_like(user_id=event.user_id, times=10)
    except ActionFailed:
        await send_like.finish(cfg.send_like_limited_message)

    await send_like.finish(random.choice(cfg.send_like_messages))


@send_face.handle()
async def _(face_msg: OneBotMessage = CommandArg()):
    try:
        face_id = int(face_msg.extract_plain_text().strip())
    except ValueError:
        await send_face.finish("请提供一个有效的表情ID")

    await send_face.finish(MessageSegment.face(face_id))
