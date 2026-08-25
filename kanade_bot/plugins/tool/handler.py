import asyncio
import base64
import binascii
import mimetypes
import random
import re
from collections.abc import Iterable
from pathlib import Path

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

from kanade_bot.utils.common import HTTPX_CLIENT
from kanade_bot.utils.onebot11 import get_image_path, send_poke, set_msg_emoji_like
from kanade_bot.utils.parse import bool_from_str, parse_arg_message

from .config import cfg, preset_reaction_cfg
from .matcher import (
    add_a_schedule,
    image_edit,
    image_generation,
    list_schedules,
    mc_skin,
    mc_status,
    pjsk_skill_multiplier,
    receive_poke,
    remove_a_schedule,
    send_a_poke,
    send_face,
    send_like,
    set_emoji_like,
    set_this_emoji_like,
    thunder_link_parse,
)
from .mcstatus import render_mc_status
from .schedule import add_schedule, print_schedules_pretty, remove_schedule

require("nonebot_plugin_localstore")


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
    except (binascii.Error, UnicodeDecodeError) as e:
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
    except (TypeError, ValueError, OSError) as e:
        logger.warning(f"查询服务器状态失败: {e}")
        await mc_status.finish("服务器查询失败")

    # 展示实际连接端口（SRV 解析后可能与输入不同）。
    image = await render_mc_status(status, address, theme)
    if isinstance(event, OneBotMessageEvent):
        # 发送图片消息
        await mc_status.finish(MessageSegment.image(image))

    # 其他平台保存图片文件
    from nonebot_plugin_localstore import get_plugin_cache_file

    image_path = get_plugin_cache_file("mc_status.png")
    image_path.write_bytes(image)
    await mc_status.finish("服务器状态已保存到 mc_status.png")


@mc_skin.handle()
async def _(event: Event, arg_msg: Message = CommandArg()):
    username = arg_msg.extract_plain_text().strip()
    if not username:
        await mc_skin.finish("请提供玩家用户名")

    resp = await HTTPX_CLIENT.get(f"https://api.mojang.com/users/profiles/minecraft/{username}")
    data = resp.json()
    if resp.status_code == 404:
        await mc_skin.finish(data["errorMessage"])
    if resp.status_code != 200:
        await mc_skin.finish("查询UUID失败")
    uuid = data["id"]

    body_data = await HTTPX_CLIENT.get(f"https://mc-api.io/render/full/{username}/java?size=256")
    if body_data.status_code != 200:
        await mc_skin.finish("获取皮肤失败")

    info_message = f"玩家: {username}\nUUID: {uuid}"
    if isinstance(event, OneBotMessageEvent):
        message = OneBotMessage(info_message)
        message += MessageSegment.image(body_data.content)
        await mc_skin.finish(message)

    await mc_skin.finish(info_message)


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


def parse_emoji_id_from_message(message: OneBotMessage) -> int | None:
    """
    从消息中解析出表情ID。支持以下格式：
    1. 回复消息中的表情
    2. 消息中包含的表情
    3. 消息中包含的单个emoji字符（部分emoji可能为多个码位组成，无法使用）
    4. 消息中包含的数字（作为表情ID）
    """
    for segment in message:
        if segment.type == "face":
            return segment.data["id"]
        if segment.type == "text":
            text: str = segment.data["text"].strip()
            if text.isnumeric():
                return int(text)
            if emoji.is_emoji(text):
                # 部分emoji可能包含变体选择器（如 \ufe0f），需要去除后再转换为码点ID
                emoji_stripped = text.replace("\ufe0f", "").replace("\ufe0e", "")
                return int.from_bytes(emoji_stripped.encode("utf-32-be"), "big")


@set_emoji_like.handle()
async def _(bot: OneBot, event: OneBotMessageEvent, arg_msg: OneBotMessage = CommandArg()):
    message_id = reply.message_id if (reply := event.reply) else event.message_id
    emoji_id = parse_emoji_id_from_message(arg_msg)
    if emoji_id is None:
        await set_emoji_like.finish("请提供单个表情或emoji（部分emoji为多个码位组成，无法使用）")

    try:
        await set_msg_emoji_like(bot, message_id, emoji_id)
    except ActionFailed:
        await set_emoji_like.finish("设置表情回应失败，可能是表情ID无效")


@set_this_emoji_like.handle()
async def _(bot: OneBot, event: OneBotMessageEvent):
    reply = event.reply
    if not reply:
        await set_this_emoji_like.finish("请回复一条消息以设置表情回应")

    message_id = reply.message_id
    emoji_id = parse_emoji_id_from_message(reply.message)
    if emoji_id is None:
        await set_this_emoji_like.finish("回复的消息中没有有效的表情或emoji")

    try:
        await set_msg_emoji_like(bot, message_id, emoji_id)
    except ActionFailed:
        await set_this_emoji_like.finish("设置表情回应失败，可能是表情ID无效")


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
    cfg = preset_reaction_cfg.instance
    if i < cfg.send_poke_probability:
        # 戳回去
        await send_poke(bot, event.user_id, event.group_id)
    await receive_poke.finish(random.choice(cfg.receive_poke_messages))


@send_like.handle()
async def _(bot: OneBot, event: OneBotMessageEvent):
    cfg = preset_reaction_cfg.instance
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


SENSENOVA_MODEL = "sensenova-u1.5-lite"
SENSENOVA_GENERATIONS_URL = "https://token.sensenova.cn/v1/images/generations"
SENSENOVA_EDITS_URL = "https://token.sensenova.cn/v1/images/edits"


class ImageCreationError(ValueError):
    """可直接反馈给用户的图片创作错误。"""


def _parse_image_args(arg_text: str) -> tuple[str, str, bool]:
    """解析“提示词 [尺寸] [是否润色]”，提示词允许包含空格。"""
    tokens = arg_text.strip().split()
    prompt_extend = False
    size = "auto"

    if tokens:
        try:
            prompt_extend = bool_from_str(tokens[-1])
        except ValueError:
            pass
        else:
            tokens.pop()

    if tokens and (
        tokens[-1].lower() == "auto"
        or re.fullmatch(r"\d+x\d+", tokens[-1], re.IGNORECASE)
    ):
        size = tokens.pop()

    prompt = " ".join(tokens).strip()
    if not prompt:
        raise ImageCreationError("请提供图片提示词。")
    return prompt, size, prompt_extend


def _image_segments(message: Iterable[MessageSegment] | None) -> list[MessageSegment]:
    if message is None:
        return []
    return [segment for segment in message if segment.type == "image"]


async def _image_data_urls(
    bot: OneBot, message: Iterable[MessageSegment] | None
) -> list[str]:
    """将 OneBot 图片消息段下载并转换为 SenseNova 接受的 Data-URL。"""
    urls: list[str] = []
    for segment in _image_segments(message):
        try:
            path = await get_image_path(bot, segment)
            data = await asyncio.to_thread(Path(path).read_bytes)
        except Exception as exc:
            raise ImageCreationError("读取图片失败，请重新发送图片。") from exc
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return urls


def _api_error(response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("msg") or error)
        return str(error)
    except (ValueError, TypeError):
        return response.text[:200] or f"HTTP {response.status_code}"


async def _request_image(url: str, payload: dict) -> list[MessageSegment]:
    config = cfg
    if not config.sensenova_api_key:
        raise ImageCreationError("未配置 SenseNova API Key。")
    try:
        response = await HTTPX_CLIENT.post(
            url,
            headers={"Authorization": f"Bearer {config.sensenova_api_key}"},
            json=payload,
        )
    except Exception as exc:
        raise ImageCreationError("图片创作请求失败，请稍后重试。") from exc
    if response.status_code >= 400:
        raise ImageCreationError(f"图片创作失败：{_api_error(response)}")
    try:
        data = response.json().get("data", [])
    except (ValueError, AttributeError) as exc:
        raise ImageCreationError("图片创作返回数据格式错误。") from exc
    if not isinstance(data, list) or not data:
        raise ImageCreationError("图片创作未返回图片。")

    result: list[MessageSegment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            try:
                image = base64.b64decode(item["b64_json"], validate=True)
            except (ValueError, binascii.Error, TypeError) as exc:
                raise ImageCreationError("图片创作返回的图片数据无效。") from exc
            result.append(MessageSegment.image(image))
        elif item.get("url"):
            result.append(MessageSegment.image(item["url"]))
    if not result:
        raise ImageCreationError("图片创作未返回可用图片。")
    return result


async def _create_image(prompt: str, size: str, prompt_extend: bool) -> list[MessageSegment]:
    return await _request_image(
        SENSENOVA_GENERATIONS_URL,
        {
            "model": SENSENOVA_MODEL,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "output_format": "png",
            "response_format": "b64_json",
            "watermark": False,
            "prompt_extend": prompt_extend,
        },
    )


async def _edit_image(
    images: list[str], prompt: str, size: str, prompt_extend: bool
) -> list[MessageSegment]:
    return await _request_image(
        SENSENOVA_EDITS_URL,
        {
            "model": SENSENOVA_MODEL,
            "images": [{"image_url": image} for image in images],
            "prompt": prompt,
            "n": 1,
            "size": size,
            "response_format": "b64_json",
            "watermark": False,
            "prompt_extend": prompt_extend,
        },
    )


def _compose_images(images: list[MessageSegment]) -> OneBotMessage:
    message = OneBotMessage()
    for image in images:
        message += image
    return message


@image_generation.handle()
async def _(bot: OneBot, arg_msg: Message = CommandArg()):
    try:
        prompt, size, prompt_extend = _parse_image_args(arg_msg.extract_plain_text())
        images = await _create_image(prompt, size, prompt_extend)
    except ImageCreationError as exc:
        await image_generation.finish(str(exc))
    await image_generation.finish(_compose_images(images))


@image_edit.handle()
async def _(
    state: T_State,
    bot: OneBot,
    event: OneBotMessageEvent,
    arg_msg: Message = CommandArg(),
):
    try:
        prompt, size, prompt_extend = _parse_image_args(arg_msg.extract_plain_text())
    except ImageCreationError as exc:
        await image_edit.finish(str(exc))

    try:
        image_urls = await _image_data_urls(
            bot, event.reply.message if event.reply else None
        )
        image_urls.extend(await _image_data_urls(bot, event.message))
    except ImageCreationError as exc:
        await image_edit.finish(str(exc))
    if image_urls:
        try:
            images = await _edit_image(image_urls, prompt, size, prompt_extend)
        except ImageCreationError as exc:
            await image_edit.finish(str(exc))
        await image_edit.finish(_compose_images(images))

    state.update(prompt=prompt, size=size, prompt_extend=prompt_extend)
    await image_edit.pause("请发送要编辑的图片（可一次发送多张）：")


@image_edit.handle()
async def _(state: T_State, bot: OneBot, event: OneBotMessageEvent):
    try:
        image_urls = await _image_data_urls(
            bot, event.reply.message if event.reply else None
        )
        image_urls.extend(await _image_data_urls(bot, event.message))
    except ImageCreationError as exc:
        await image_edit.finish(str(exc))
    if not image_urls:
        await image_edit.finish("未找到图片，请发送图片后重试。")
    try:
        images = await _edit_image(
            image_urls,
            state["prompt"],
            state.get("size", "auto"),
            state.get("prompt_extend", False),
        )
    except ImageCreationError as exc:
        await image_edit.finish(str(exc))
    await image_edit.finish(_compose_images(images))
