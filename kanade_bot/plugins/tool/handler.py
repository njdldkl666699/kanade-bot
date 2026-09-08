import asyncio
import base64
import binascii
import random
import re
from collections.abc import Iterable
from io import BytesIO

import emoji
import magic
from httpx import HTTPError, Response
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
from PIL import Image

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
    video_generation,
)
from .mcstatus import render_mc_status
from .schedule import add_schedule, print_schedules_pretty, remove_schedule

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_cache_file

require("crystal")
from kanade_bot.plugins.crystal import (
    HandlerKeyEnum,
    check_user_crystal,
    finish_fail_consume,
    succeed_consume,
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

AGNES_IMAGE_MODEL = "agnes-image-2.5-flash"
AGNES_VIDEO_MODEL = "agnes-video-2.5-flash"
AGNES_IMAGE_RATIOS = frozenset({"1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"})
"""Agnes 图片创作支持的宽高比"""
AGNES_VIDEO_RATIOS = frozenset({"21:9", "16:9", "4:3", "1:1", "3:4", "9:16"})
"""Agnes 视频生成支持的画幅"""
AGNES_TIER_IMAGE_SIZES = {
    "1K": "1024x1024",
    "2K": "2048x2048",
    "3K": "3072x3072",
    "4K": "4096x4096",
}
"""Agnes 档位式尺寸到方形精确尺寸的映射，供 SenseNova 使用"""
AGNES_VIDEO_POLL_INTERVAL = 3.0
"""查询视频任务进度的间隔秒数"""
AGNES_VIDEO_POLL_TIMEOUT = 600.0
"""等待视频任务完成的最长秒数"""


class CreationError(ValueError):
    """可直接反馈给用户的媒体创作错误。"""


def _parse_image_args(arg_text: str) -> tuple[str, str, str | None, bool]:
    """解析“提示词 [尺寸] [宽高比] [是否润色]”，提示词允许包含空格。

    尺寸支持`auto`、如`1024x768`的精确尺寸，以及`1K`-`4K`档位（档位仅Agnes支持）；
    宽高比支持`1:1`、`3:4`、`4:3`、`16:9`、`9:16`、`2:3`、`3:2`、`21:9`（仅Agnes支持）。
    """
    tokens = arg_text.strip().split()
    prompt_extend = False
    ratio: str | None = None
    size = "auto"

    if tokens:
        try:
            prompt_extend = bool_from_str(tokens[-1])
        except ValueError:
            pass
        else:
            tokens.pop()

    if tokens and tokens[-1] in AGNES_IMAGE_RATIOS:
        ratio = tokens.pop()

    if tokens:
        last = tokens[-1]
        if re.fullmatch(r"[1-4][kK]", last):
            size = last.upper()
            tokens.pop()
        elif last.lower() == "auto" or re.fullmatch(r"\d+x\d+", last, re.IGNORECASE):
            size = last
            tokens.pop()

    prompt = " ".join(tokens).strip()
    if not prompt:
        raise CreationError("请提供图片提示词。")
    return prompt, size, ratio, prompt_extend


def _image_segments(message: Iterable[MessageSegment] | None) -> list[MessageSegment]:
    if message is None:
        return []
    return [segment for segment in message if segment.type == "image"]


async def _image_data_urls(bot: OneBot, message: Iterable[MessageSegment] | None) -> list[str]:
    """将 OneBot 图片消息段下载并转换为 API 接受的 Data-URL。"""
    urls: list[str] = []
    for segment in _image_segments(message):
        try:
            path = await get_image_path(bot, segment)
            data = await asyncio.to_thread(path.read_bytes)
        except Exception as exc:
            raise CreationError("读取图片失败，请重新发送图片。") from exc

        mime = magic.from_buffer(data, mime=True)
        if mime == "image/gif":
            # 取 GIF 的第一帧并转换为 PNG
            try:
                with Image.open(BytesIO(data)) as img:
                    frame = img.convert("RGBA")
                    output = BytesIO()
                    frame.save(output, format="PNG")
                    data = output.getvalue()
                mime = "image/png"
            except Exception as e:
                raise CreationError("无法处理 GIF 图片，请尝试使用 PNG 或 JPG 格式。") from e

        urls.append(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}")
    return urls


def _api_error(response: Response) -> str:
    try:
        payload = response.json()
        error = payload.get("error", payload)
        if isinstance(error, dict):
            return str(error.get("message") or error.get("msg") or error)
        return str(error)
    except (ValueError, TypeError):
        return response.text[:200] or f"HTTP {response.status_code}"


def _parse_image_response(response: Response) -> list[MessageSegment]:
    if response.status_code >= 400:
        raise CreationError(f"图片创作失败：{_api_error(response)}")
    try:
        data = response.json().get("data", [])
    except (ValueError, AttributeError) as exc:
        raise CreationError("图片创作返回数据格式错误。") from exc
    if not isinstance(data, list) or not data:
        raise CreationError("图片创作未返回图片。")

    result: list[MessageSegment] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("b64_json"):
            try:
                image = base64.b64decode(item["b64_json"], validate=True)
            except (ValueError, binascii.Error, TypeError) as exc:
                raise CreationError("图片创作返回的图片数据无效。") from exc
            result.append(MessageSegment.image(image))
        elif item.get("url"):
            result.append(MessageSegment.image(item["url"]))
    if not result:
        raise CreationError("图片创作未返回可用图片。")
    return result


async def _sensenova_request_image(url: str, payload: dict) -> list[MessageSegment]:
    if not cfg.sensenova_api_key:
        raise CreationError("未配置 SenseNova API Key。")
    try:
        response = await HTTPX_CLIENT.post(
            url,
            headers={"Authorization": f"Bearer {cfg.sensenova_api_key}"},
            json=payload,
            timeout=60,
        )
    except Exception as exc:
        logger.warning(f"请求 SenseNova API 失败: {exc}")
        raise CreationError("图片创作请求失败，请稍后重试。") from exc
    return _parse_image_response(response)


def _agnes_auth_headers() -> dict[str, str]:
    if not cfg.agnes_api_key:
        raise CreationError("未配置 Agnes API Key。")
    return {"Authorization": f"Bearer {cfg.agnes_api_key}"}


async def _agnes_request_image(payload: dict) -> list[MessageSegment]:
    url = f"{cfg.agnes_base_url.rstrip('/')}/images/generations"
    try:
        response = await HTTPX_CLIENT.post(
            url,
            headers=_agnes_auth_headers(),
            json=payload,
            timeout=120,
        )
    except Exception as exc:
        logger.warning(f"请求 Agnes API 失败: {exc}")
        raise CreationError("图片创作请求失败，请稍后重试。") from exc
    return _parse_image_response(response)


def _sensenova_image_size(size: str, ratio: str | None) -> str:
    """将解析后的尺寸参数转换为 SenseNova 接受的精确尺寸。"""
    if ratio:
        raise CreationError("SenseNova 不支持指定宽高比，请使用如 1024x1024 的尺寸。")
    return AGNES_TIER_IMAGE_SIZES.get(size, size)


def _agnes_image_size(size: str) -> str:
    """将解析后的尺寸参数转换为 Agnes 接受的尺寸。"""
    return "1K" if size == "auto" else size


async def _create_image(
    prompt: str, size: str, ratio: str | None, prompt_extend: bool
) -> list[MessageSegment]:
    if cfg.image_provider == "agnes":
        payload: dict = {
            "model": AGNES_IMAGE_MODEL,
            "prompt": prompt,
            "size": _agnes_image_size(size),
            "return_base64": True,
        }
        if ratio:
            payload["ratio"] = ratio
        return await _agnes_request_image(payload)

    return await _sensenova_request_image(
        SENSENOVA_GENERATIONS_URL,
        {
            "model": SENSENOVA_MODEL,
            "prompt": prompt,
            "n": 1,
            "size": _sensenova_image_size(size, ratio),
            "output_format": "png",
            "response_format": "b64_json",
            "watermark": False,
            "prompt_extend": prompt_extend,
        },
    )


async def _edit_image(
    images: list[str], prompt: str, size: str, ratio: str | None, prompt_extend: bool
) -> list[MessageSegment]:
    if cfg.image_provider == "agnes":
        payload: dict = {
            "model": AGNES_IMAGE_MODEL,
            "prompt": prompt,
            "size": _agnes_image_size(size),
            "extra_body": {
                "image": images,
                "response_format": "b64_json",
            },
        }
        if ratio:
            payload["ratio"] = ratio
        return await _agnes_request_image(payload)

    return await _sensenova_request_image(
        SENSENOVA_EDITS_URL,
        {
            "model": SENSENOVA_MODEL,
            "images": [{"image_url": image} for image in images],
            "prompt": prompt,
            "n": 1,
            "size": _sensenova_image_size(size, ratio),
            "response_format": "b64_json",
            "watermark": False,
            "prompt_extend": prompt_extend,
        },
    )


def _compose_images(message_id: int, images: list[MessageSegment]):
    message = MessageSegment.reply(message_id)
    for image in images:
        message += image
    return message


@image_generation.handle()
async def _(event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    kpu = (HandlerKeyEnum.IMAGE_GENERATION, "onebot", event.get_user_id())
    if not check_user_crystal(*kpu):
        await finish_fail_consume(image_generation, *kpu)

    try:
        prompt, size, ratio, prompt_extend = _parse_image_args(arg_msg.extract_plain_text())
        images = await _create_image(prompt, size, ratio, prompt_extend)
    except CreationError as exc:
        await image_generation.finish(str(exc))

    succeed_consume(*kpu)
    await image_generation.finish(_compose_images(event.message_id, images))


@image_edit.handle()
async def _(
    state: T_State,
    bot: OneBot,
    event: OneBotMessageEvent,
    arg_msg: Message = CommandArg(),
):
    kpu = (HandlerKeyEnum.IMAGE_EDIT, "onebot", event.get_user_id())
    if not check_user_crystal(*kpu):
        await finish_fail_consume(image_edit, *kpu)

    try:
        prompt, size, ratio, prompt_extend = _parse_image_args(arg_msg.extract_plain_text())
    except CreationError as exc:
        await image_edit.finish(str(exc))

    try:
        image_urls = await _image_data_urls(bot, event.reply.message if event.reply else None)
        image_urls.extend(await _image_data_urls(bot, event.message))
    except CreationError as exc:
        await image_edit.finish(str(exc))
    if image_urls:
        try:
            images = await _edit_image(image_urls, prompt, size, ratio, prompt_extend)
        except CreationError as exc:
            await image_edit.finish(str(exc))
        succeed_consume(*kpu)
        await image_edit.finish(_compose_images(event.message_id, images))

    state.update(prompt=prompt, size=size, ratio=ratio, prompt_extend=prompt_extend)
    await image_edit.pause("请发送要编辑的图片（可一次发送多张）：")


@image_edit.handle()
async def _(state: T_State, bot: OneBot, event: OneBotMessageEvent):
    try:
        image_urls = await _image_data_urls(bot, event.reply.message if event.reply else None)
        image_urls.extend(await _image_data_urls(bot, event.message))
    except CreationError as exc:
        await image_edit.finish(str(exc))
    if not image_urls:
        await image_edit.finish("未找到图片，请发送图片后重试。")
    try:
        images = await _edit_image(
            image_urls,
            state["prompt"],
            state.get("size", "auto"),
            state.get("ratio"),
            state.get("prompt_extend", False),
        )
    except CreationError as exc:
        await image_edit.finish(str(exc))

    succeed_consume(HandlerKeyEnum.IMAGE_EDIT, "onebot", event.get_user_id())
    await image_edit.finish(_compose_images(event.message_id, images))


VIDEO_IMAGE_MODE_PROMPTS = {
    "keyframe": "请发送首帧图片（如需尾帧可一次发送两张，第一张为首帧，第二张为尾帧）：",
    "reference": "请发送参考图片（最多5张）：",
}


def _parse_video_args(arg_text: str) -> tuple[str, str, str, str | None]:
    """解析“提示词 [秒数] [画幅] [模式]”，提示词允许包含空格。

    秒数支持4-12；画幅支持`21:9`、`16:9`、`4:3`、`1:1`、`3:4`、`9:16`；
    模式支持`keyframe`（首尾帧控制）和`reference`（参考图生成），不指定时自动判断。
    """
    tokens = arg_text.strip().split()
    mode: str | None = None
    ratio = "16:9"
    seconds = "5"

    if tokens and tokens[-1].lower() in ("keyframe", "reference"):
        mode = tokens.pop().lower()
    if tokens and tokens[-1] in AGNES_VIDEO_RATIOS:
        ratio = tokens.pop()
    if tokens and tokens[-1].isdigit():
        seconds_value = int(tokens[-1])
        if not 4 <= seconds_value <= 12:
            raise CreationError("视频时长仅支持4-12秒。")
        seconds = tokens.pop()

    prompt = " ".join(tokens).strip()
    if not prompt:
        raise CreationError("请提供视频提示词。")
    return prompt, seconds, ratio, mode


def _build_video_payload(
    prompt: str, seconds: str, ratio: str, mode: str, image_urls: list[str]
) -> dict:
    payload: dict = {
        "model": AGNES_VIDEO_MODEL,
        "prompt": prompt,
        "mode": mode,
        "seconds": seconds,
        "size": "720P",
        "aspect_ratio": ratio,
    }
    if mode == "keyframe":
        payload["first_frame"] = image_urls[0]
        if len(image_urls) > 1:
            payload["last_frame"] = image_urls[1]
    elif mode == "reference":
        payload["images"] = image_urls
    return payload


def _validate_video_args(mode: str, image_urls: list[str]) -> None:
    if mode == "keyframe":
        if not image_urls:
            raise CreationError("首尾帧模式需要至少提供一张首帧图片。")
        if len(image_urls) > 2:
            raise CreationError("首尾帧模式最多支持2张图片（首帧和尾帧）。")
    elif mode == "reference":
        if not image_urls:
            raise CreationError("参考模式需要至少提供一张参考图片。")
        if len(image_urls) > 5:
            raise CreationError("参考模式最多支持5张参考图片。")


async def _wait_video_result(video_id: str) -> str:
    """轮询 Agnes 视频任务直至完成，返回视频URL。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + AGNES_VIDEO_POLL_TIMEOUT
    delay = AGNES_VIDEO_POLL_INTERVAL
    query_url = f"{cfg.agnes_base_url.rstrip('/').removesuffix('/v1')}/agnesapi"
    while True:
        await asyncio.sleep(delay)
        try:
            response = await HTTPX_CLIENT.get(
                query_url,
                headers=_agnes_auth_headers(),
                params={"video_id": video_id, "model_name": AGNES_VIDEO_MODEL},
                timeout=30,
            )
        except HTTPError as exc:
            logger.warning(f"查询 Agnes 视频任务失败: {exc}")
        else:
            if response.status_code == 429:
                # 请求频率超限，指数退避
                delay = min(delay * 2, 30.0)
                continue
            if response.status_code >= 400:
                raise CreationError(f"查询视频任务失败：{_api_error(response)}")
            try:
                data = response.json()
            except ValueError:
                data = {}
            status = data.get("status")
            if status == "completed":
                url = (data.get("metadata") or {}).get("url")
                if not url:
                    raise CreationError("视频任务已完成但未返回视频地址。")
                return str(url)
            if status == "failed":
                message = (data.get("error") or {}).get("message") or "未知原因"
                raise CreationError(f"视频生成失败：{message}")
        if loop.time() > deadline:
            raise CreationError("视频生成超时，请稍后重试。")


async def _create_and_wait_video(payload: dict) -> str:
    """创建 Agnes 视频任务并轮询直至完成，返回视频URL。"""
    try:
        response = await HTTPX_CLIENT.post(
            f"{cfg.agnes_base_url.rstrip('/')}/videos",
            headers=_agnes_auth_headers(),
            json=payload,
            timeout=60,
        )
    except Exception as exc:
        logger.warning(f"请求 Agnes API 失败: {exc}")
        raise CreationError("视频生成请求失败，请稍后重试。") from exc
    if response.status_code >= 400:
        raise CreationError(f"视频任务创建失败：{_api_error(response)}")
    try:
        video_id = response.json().get("video_id")
    except ValueError as exc:
        raise CreationError("视频任务返回数据格式错误。") from exc
    if not video_id:
        raise CreationError("视频任务创建未返回任务ID。")
    return await _wait_video_result(video_id)


async def _download_video(url: str) -> bytes:
    try:
        response = await HTTPX_CLIENT.get(url, timeout=300)
        response.raise_for_status()
        return response.content
    except Exception as exc:
        logger.warning(f"下载生成的视频失败: {exc}")
        raise CreationError(f"视频下载失败，可稍后从此链接查看：{url}") from exc


async def _generate_video_message(
    kpu: tuple,
    event: OneBotMessageEvent,
    prompt: str,
    seconds: str,
    ratio: str,
    mode: str | None,
    image_urls: list[str],
):
    """构建视频任务请求并等待结果，返回待发送的消息。"""
    if mode is None:
        # 未显式指定模式时，附带图片则以参考模式生成，否则以文本模式生成
        mode = "reference" if image_urls else "text"
    _validate_video_args(mode, image_urls)

    payload = _build_video_payload(prompt, seconds, ratio, mode, image_urls)
    video_url = await _create_and_wait_video(payload)

    succeed_consume(*kpu)
    message = MessageSegment.reply(event.message_id)
    message += MessageSegment.video(await _download_video(video_url))
    return message


@video_generation.handle()
async def _(
    state: T_State,
    bot: OneBot,
    event: OneBotMessageEvent,
    arg_msg: Message = CommandArg(),
):
    kpu = (HandlerKeyEnum.VIDEO_GENERATION, "onebot", event.get_user_id())
    if not check_user_crystal(*kpu):
        await finish_fail_consume(video_generation, *kpu)

    try:
        prompt, seconds, ratio, mode = _parse_video_args(arg_msg.extract_plain_text())
    except CreationError as exc:
        await video_generation.finish(str(exc))

    try:
        image_urls = await _image_data_urls(bot, event.reply.message if event.reply else None)
        image_urls.extend(await _image_data_urls(bot, event.message))
    except CreationError as exc:
        await video_generation.finish(str(exc))

    if mode and not image_urls:
        # 显式指定了需要图片的模式但未提供图片，等待用户发送
        state.update(prompt=prompt, seconds=seconds, ratio=ratio, mode=mode)
        await video_generation.pause(VIDEO_IMAGE_MODE_PROMPTS[mode])

    try:
        message = await _generate_video_message(
            kpu, event, prompt, seconds, ratio, mode, image_urls
        )
    except CreationError as exc:
        await video_generation.finish(str(exc))
    await video_generation.finish(message)


@video_generation.handle()
async def _(state: T_State, bot: OneBot, event: OneBotMessageEvent):
    try:
        image_urls = await _image_data_urls(bot, event.reply.message if event.reply else None)
        image_urls.extend(await _image_data_urls(bot, event.message))
    except CreationError as exc:
        await video_generation.finish(str(exc))
    if not image_urls:
        await video_generation.finish("未找到图片，请发送图片后重试。")

    try:
        message = await _generate_video_message(
            (HandlerKeyEnum.VIDEO_GENERATION, "onebot", event.get_user_id()),
            event,
            state["prompt"],
            state["seconds"],
            state["ratio"],
            state.get("mode", "reference"),
            image_urls,
        )
    except CreationError as exc:
        await video_generation.finish(str(exc))
    await video_generation.finish(message)
