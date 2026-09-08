import asyncio
import base64
import binascii
import re
from collections.abc import Iterable
from io import BytesIO

import magic
from httpx import HTTPError, Response
from nonebot import logger, require
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from PIL import Image

from kanade_bot.utils.common import HTTPX_CLIENT
from kanade_bot.utils.onebot11 import get_image_path
from kanade_bot.utils.parse import bool_from_str

from .config import cfg

require("crystal")
from kanade_bot.plugins.crystal import succeed_consume

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


def parse_image_args(arg_text: str) -> tuple[str, str, str | None, bool]:
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


async def image_data_urls(bot: OneBot, message: Iterable[MessageSegment] | None) -> list[str]:
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


async def create_image(
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


async def edit_image(
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


def compose_images(message_id: int, images: list[MessageSegment]):
    message = MessageSegment.reply(message_id)
    for image in images:
        message += image
    return message


VIDEO_IMAGE_MODE_PROMPTS = {
    "keyframe": "请发送首帧图片（如需尾帧可一次发送两张，第一张为首帧，第二张为尾帧）：",
    "reference": "请发送参考图片（最多5张）：",
}


def parse_video_args(arg_text: str) -> tuple[str, str, str, str | None]:
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
                # 兼容 url 在顶层（实测）与 metadata.url（文档）两种返回格式
                url = data.get("url") or (data.get("metadata") or {}).get("url")
                if not url:
                    raise CreationError("视频任务已完成但未返回视频地址。")
                return str(url)
            if status == "failed":
                error = data.get("error")
                if isinstance(error, dict):
                    message = error.get("message") or "未知原因"
                elif error:
                    message = str(error)
                else:
                    message = "未知原因"
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
        data = response.json()
    except ValueError as exc:
        raise CreationError("视频任务返回数据格式错误。") from exc
    # 优先使用 video_id，缺失时回退到 id/task_id（实测响应直接以任务ID查询）
    video_id = data.get("video_id") or data.get("id") or data.get("task_id")
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


async def generate_video_message(
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
