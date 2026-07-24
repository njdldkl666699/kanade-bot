import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent, MessageSegment
from nonebot.params import CommandArg
from PIL import Image, UnidentifiedImageError

from .matcher import back, fan, flow, mid, mirror, rotate, speed

# QQ 客户端对 GIF 的帧间隔支持并不一致。20ms 是参考实现使用的
# 最小间隔；更快时通过抽帧实现，避免客户端把间隔强制改大后反而变慢。
MIN_FRAME_DURATION = 20
EFFECT_BASE_FRAMES = 20
EFFECT_SPEED_MIN = 0.2
EFFECT_SPEED_MAX = 5.0


class ImageToolError(ValueError):
    """可以直接回复给用户的图片处理错误。"""


@dataclass(slots=True)
class Animation:
    frames: list[Image.Image]
    durations: list[int]
    loop: int = 0


def _arguments(arg_msg: Message) -> list[str]:
    return arg_msg.extract_plain_text().strip().lower().split()


async def _get_reply_image(bot: Bot, event: MessageEvent) -> bytes:
    """获取 OneBot v11 引用消息中的第一张图片。"""
    if event.reply is None:
        raise ImageToolError("请引用一条包含图片的消息。")

    image_segment = next(
        (segment for segment in event.reply.message if segment.type == "image"),
        None,
    )
    if image_segment is None:
        raise ImageToolError("引用的消息中没有图片。")

    image_file = image_segment.data.get("file")
    image_url = image_segment.data.get("url")

    # 优先使用 OneBot 的 get_image API。它既能处理客户端内部的图片标识，
    # 也能避免再次从公网下载已经落盘的图片。
    if image_file:
        try:
            image_info = await bot.get_image(file=image_file)
            local_file = image_info.get("file")
            image_url = image_info.get("url") or image_url
            if local_file:
                path = Path(local_file)
                if path.is_file():
                    return await asyncio.to_thread(path.read_bytes)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"imgtool 调用 get_image 失败，将尝试直接读取图片：{e}")

    # 某些实现会直接把本地路径或 URL 放在 image.file 中。
    if image_file:
        if str(image_file).startswith(("http://", "https://")):
            image_url = str(image_file)
        else:
            try:
                path = Path(image_file)
                if path.is_file():
                    return await asyncio.to_thread(path.read_bytes)
            except OSError:
                pass

    if image_url and str(image_url).startswith(("http://", "https://")):
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(str(image_url))
                response.raise_for_status()
                return response.content
        except httpx.HTTPError as exc:
            logger.warning(f"imgtool 下载引用图片失败：{exc}")

    raise ImageToolError("获取引用图片失败。")


def _decode_image(data: bytes) -> Animation:
    try:
        image = Image.open(BytesIO(data))
    except (UnidentifiedImageError, OSError) as exc:
        raise ImageToolError("无法读取引用的图片。") from exc

    frames: list[Image.Image] = []
    durations: list[int] = []
    frame_count = getattr(image, "n_frames", 1)
    default_duration = int(image.info.get("duration") or 100)

    try:
        for index in range(frame_count):
            image.seek(index)
            # convert 会复制当前已合成的完整画面，能够正确处理 GIF 的局部帧。
            frames.append(image.convert("RGBA"))
            durations.append(max(1, int(image.info.get("duration") or default_duration)))
    except (EOFError, OSError) as exc:
        raise ImageToolError("读取图片帧失败。") from exc

    if not frames:
        raise ImageToolError("图片中没有可处理的画面。")
    return Animation(frames, durations, int(image.info.get("loop", 0)))


def _encode_png(image: Image.Image) -> bytes:
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _encode_gif(animation: Animation) -> bytes:
    if len(animation.frames) < 2:
        raise ImageToolError("处理后动图只剩一帧，请降低速度后重试。")

    output = BytesIO()
    first, *remaining = (frame.convert("RGBA") for frame in animation.frames)
    first.save(
        output,
        format="GIF",
        save_all=True,
        append_images=remaining,
        duration=animation.durations,
        loop=animation.loop,
        disposal=2,
    )
    return output.getvalue()


def _encode_like_input(animation: Animation, was_animated: bool) -> bytes:
    if was_animated:
        return _encode_gif(animation)
    return _encode_png(animation.frames[0])


def _map_frames(
    data: bytes,
    operation: Callable[[Image.Image], Image.Image],
) -> bytes:
    animation = _decode_image(data)
    was_animated = len(animation.frames) > 1
    animation.frames = [operation(frame) for frame in animation.frames]
    return _encode_like_input(animation, was_animated)


def _mirror(data: bytes, args: list[str]) -> bytes:
    if args not in ([], ["v"]):
        raise ImageToolError("用法：/镜像 [v]")
    method = Image.Transpose.FLIP_TOP_BOTTOM if args else Image.Transpose.FLIP_LEFT_RIGHT
    return _map_frames(data, lambda frame: frame.transpose(method))


def _rotate(data: bytes, args: list[str]) -> bytes:
    if len(args) != 1:
        raise ImageToolError("用法：/旋转 <角度>")
    try:
        degree = int(args[0])
    except ValueError as exc:
        raise ImageToolError("角度必须是整数。") from exc
    return _map_frames(data, lambda frame: frame.rotate(degree, expand=True))


def _back(data: bytes, args: list[str]) -> bytes:
    if args:
        raise ImageToolError("用法：/倒放")
    animation = _decode_image(data)
    if len(animation.frames) < 2:
        raise ImageToolError("倒放仅支持动图。")
    animation.frames.reverse()
    animation.durations.reverse()
    return _encode_gif(animation)


def _parse_speed(args: list[str]) -> tuple[float, bool]:
    if len(args) != 1:
        raise ImageToolError("用法：/倍速 <倍速或帧时长>")

    value = args[0]
    if value.endswith("x"):
        try:
            factor = float(value[:-1])
        except ValueError as exc:
            raise ImageToolError("速度格式无效，例如：2x、-2x 或 50。") from exc
        reverse = factor < 0
        factor = abs(factor)
        if not 0.01 <= factor <= 100:
            raise ImageToolError("加速倍率必须在 0.01x 到 100x 之间。")
        return factor, reverse

    try:
        duration = int(value)
    except ValueError as exc:
        raise ImageToolError("速度格式无效，例如：2x、-2x 或 50。") from exc
    reverse = duration < 0
    duration = abs(duration)
    if not 1 <= duration <= 1000:
        raise ImageToolError("帧时长必须在 1ms 到 1000ms 之间。")
    # 负值沿用参考实现的行为：设置帧时长并同时倒放。
    return -float(duration), reverse


def _speed(data: bytes, args: list[str]) -> bytes:
    animation = _decode_image(data)
    if len(animation.frames) < 2:
        raise ImageToolError("倍速仅支持动图。")

    value, reverse = _parse_speed(args)
    if value > 0:
        target_duration = animation.durations[0] / value
    else:
        target_duration = -value

    # 低于客户端可稳定显示的帧间隔时进行等间隔抽帧。
    interval = max(1, math.ceil(MIN_FRAME_DURATION / target_duration))
    new_frames = animation.frames[::interval]
    if len(new_frames) < 2:
        raise ImageToolError("速度过快，处理后动图不足两帧。")

    duration = max(MIN_FRAME_DURATION, int(target_duration * interval))
    if reverse:
        new_frames.reverse()
    return _encode_gif(Animation(new_frames, [duration] * len(new_frames), animation.loop))


def _symmetrize_frame(frame: Image.Image, mode: str) -> Image.Image:
    width, height = frame.size
    result = Image.new("RGBA", frame.size)

    if mode in {"h", "hr"}:
        # 奇数宽度保留中线像素，避免参考实现右/下边缘出现透明线。
        half = (width + 1) // 2
        if mode == "h":
            source = frame.crop((0, 0, half, height))
        else:
            source = frame.crop((width - half, 0, width, height)).transpose(
                Image.Transpose.FLIP_LEFT_RIGHT
            )
        mirrored = source.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        if width % 2:
            mirrored = mirrored.crop((1, 0, mirrored.width, mirrored.height))
        result.paste(source, (0, 0))
        result.paste(mirrored, (half, 0))
    else:
        half = (height + 1) // 2
        if mode == "v":
            source = frame.crop((0, 0, width, half))
        else:
            source = frame.crop((0, height - half, width, height)).transpose(
                Image.Transpose.FLIP_TOP_BOTTOM
            )
        mirrored = source.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
        if height % 2:
            mirrored = mirrored.crop((0, 1, mirrored.width, mirrored.height))
        result.paste(source, (0, 0))
        result.paste(mirrored, (0, half))
    return result


def _direction(args: list[str], *, allow_vertical: bool) -> str:
    allowed = {"r", "v"} if allow_vertical else {"r"}
    direction_args = [arg for arg in args if not arg.endswith("x")]
    if any(arg not in allowed for arg in direction_args):
        raise ImageToolError("方向参数无效。")
    if len(direction_args) != len(set(direction_args)):
        raise ImageToolError("方向参数不能重复。")
    return ("v" if "v" in direction_args else "h") + ("r" if "r" in direction_args else "")


def _mid(data: bytes, args: list[str]) -> bytes:
    if len(args) > 2 or any(arg.endswith("x") for arg in args):
        raise ImageToolError("用法：/对称 [r|v|v r]")
    mode = _direction(args, allow_vertical=True)
    return _map_frames(data, lambda frame: _symmetrize_frame(frame, mode))


def _effect_args(args: list[str], *, allow_vertical: bool) -> tuple[str, float]:
    max_args = 3 if allow_vertical else 2
    if len(args) > max_args:
        raise ImageToolError("参数过多。")

    speed_args = [arg for arg in args if arg.endswith("x")]
    if len(speed_args) > 1:
        raise ImageToolError("只能指定一个速度。")
    try:
        effect_speed = float(speed_args[0][:-1]) if speed_args else 1.0
    except ValueError as exc:
        raise ImageToolError("速度格式无效，例如：2x。") from exc
    if not EFFECT_SPEED_MIN <= effect_speed <= EFFECT_SPEED_MAX:
        raise ImageToolError("速度必须在 0.2x 到 5x 之间。")

    return _direction(args, allow_vertical=allow_vertical), effect_speed


def _effect_sources(animation: Animation, frame_count: int) -> list[Image.Image]:
    """让流动/转动也能保留引用动图本身的画面变化。"""
    return [
        animation.frames[index * len(animation.frames) // frame_count]
        for index in range(frame_count)
    ]


def _flow(data: bytes, args: list[str]) -> bytes:
    mode, effect_speed = _effect_args(args, allow_vertical=True)
    animation = _decode_image(data)
    frame_count = max(2, int(EFFECT_BASE_FRAMES / effect_speed))
    frames: list[Image.Image] = []

    for index, source in enumerate(_effect_sources(animation, frame_count)):
        source = source.convert("RGBA")
        width, height = source.size
        result = Image.new("RGBA", source.size)
        if mode.startswith("h"):
            offset = int(index * width / frame_count)
            if mode.endswith("r"):
                offset = -offset
            result.paste(source, (offset, 0))
            result.paste(source, (offset - width if offset > 0 else offset + width, 0))
        else:
            offset = int(index * height / frame_count)
            if mode.endswith("r"):
                offset = -offset
            result.paste(source, (0, offset))
            result.paste(source, (0, offset - height if offset > 0 else offset + height))
        frames.append(result)

    return _encode_gif(Animation(frames, [MIN_FRAME_DURATION] * frame_count, animation.loop))


def _fan(data: bytes, args: list[str]) -> bytes:
    mode, effect_speed = _effect_args(args, allow_vertical=False)
    animation = _decode_image(data)
    frame_count = max(2, int(EFFECT_BASE_FRAMES / effect_speed))
    frames: list[Image.Image] = []

    for index, source in enumerate(_effect_sources(animation, frame_count)):
        # Pillow 的正角度为逆时针，和帮助文档的默认方向一致。
        angle = 360 * index / frame_count
        if mode.endswith("r"):
            angle = -angle
        frames.append(source.convert("RGBA").rotate(angle, expand=False))

    return _encode_gif(Animation(frames, [MIN_FRAME_DURATION] * frame_count, animation.loop))


async def _handle(
    matcher,
    bot: Bot,
    event: MessageEvent,
    arg_msg: Message,
    operation: Callable[[bytes, list[str]], bytes],
) -> None:
    try:
        image_data = await _get_reply_image(bot, event)
        result = await asyncio.to_thread(operation, image_data, _arguments(arg_msg))
    except ImageToolError as e:
        await matcher.finish(str(e))
    except Exception as e:  # noqa: BLE001
        logger.exception("imgtool 处理图片失败")
        await matcher.finish(f"处理图片失败：{e}")
    else:  # 防止Pylance误报
        await matcher.finish(MessageSegment.image(result))


@mirror.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(mirror, bot, event, arg_msg, _mirror)


@rotate.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(rotate, bot, event, arg_msg, _rotate)


@back.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(back, bot, event, arg_msg, _back)


@speed.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(speed, bot, event, arg_msg, _speed)


@mid.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(mid, bot, event, arg_msg, _mid)


@flow.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(flow, bot, event, arg_msg, _flow)


@fan.handle()
async def _(bot: Bot, event: MessageEvent, arg_msg: Message = CommandArg()):
    await _handle(fan, bot, event, arg_msg, _fan)
