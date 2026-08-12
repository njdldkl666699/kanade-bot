import asyncio
import random
import re
from pathlib import Path

from nonebot import get_plugin_config, logger
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.exception import NetworkError
from nonebot.params import CommandArg, EventMessage
from nonebot.typing import T_State
from send2trash import send2trash

from kanade_bot.utils.common import HTTPX_CLIENT
from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme, get_image_local
from kanade_bot.utils.parse import get_forward_message_events, parse_arg_message
from kanade_bot.utils.schema import KanadeConfig

from .config import cfg, gallery_name_data
from .gallery import (
    add_pictures,
    get_gallery_name,
    get_picture_by_id,
    invalidate_gallery_render_cache,
    remove_gallery_from_index,
    remove_picture_from_index,
    render_gallery_overview,
    render_gallery_thumbnails,
)
from .matcher import (
    add_gallery,
    add_gallery_alias,
    add_picture,
    gallery_pictures,
    get_picture,
    remove_gallery,
    remove_gallery_alias,
    remove_picture,
)


@add_gallery.handle()
async def _(arg_msg: Message = CommandArg()):
    name = arg_msg.extract_plain_text().strip()
    if not name:
        await add_gallery.finish("请提供画廊名称。")

    v = gallery_name_data.instance
    if name in v.name_to_aliases:
        await add_gallery.finish(f"画廊 {name} 已存在。")

    # 创建画廊目录
    gallery_dir = cfg.data_dir_path / name
    try:
        gallery_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        await add_gallery.finish(f"画廊目录 {gallery_dir} 已存在，无法创建。")
    except OSError as e:
        logger.exception(f"创建画廊目录 {gallery_dir} 失败：{e}")
        await add_gallery.finish(f"创建画廊目录失败：{e}")

    # 更新索引
    v.name_to_aliases[name] = []
    gallery_name_data.save_to_file()
    invalidate_gallery_render_cache()
    await add_gallery.finish(f"成功添加画廊：{name}")


@remove_gallery.handle()
async def _(arg_msg: Message = CommandArg()):
    name = arg_msg.extract_plain_text().strip()
    if not name:
        await remove_gallery.finish("请提供画廊名称。")

    v = gallery_name_data.instance
    if name not in v.name_to_aliases:
        await remove_gallery.finish(f"画廊 {name} 不存在。")

    # 将画廊目录移至废纸篓
    gallery_dir = cfg.data_dir_path / name
    try:
        send2trash(gallery_dir)
    except OSError as e:
        logger.exception(f"删除画廊目录 {gallery_dir} 失败：{e}")
        await remove_gallery.finish(f"删除画廊目录失败：{e}")

    # 更新索引
    aliases = v.name_to_aliases.pop(name, [])
    for alias in aliases:
        v.alias_to_name.pop(alias, None)
    gallery_name_data.save_to_file()
    remove_gallery_from_index(name)
    invalidate_gallery_render_cache()
    invalidate_gallery_render_cache(name)
    await remove_gallery.finish(f"成功删除画廊：{name}")


@add_gallery_alias.handle()
async def _(arg_msg: Message = CommandArg()):
    args = parse_arg_message(
        arg_msg.extract_plain_text().strip(),
        {"name": str, "alias": str},
        maxsplit=1,
    )
    name: str | None = args["name"]
    alias: str | None = args["alias"]

    if not name or not alias:
        await add_gallery_alias.finish("请提供画廊名称和别名，格式：<画廊名称> <别名>")
    v = gallery_name_data.instance
    if name not in v.name_to_aliases:
        await add_gallery_alias.finish(f"画廊 {name} 不存在。")
    if alias in v.name_to_aliases:
        # 别名不能与现有画廊名称冲突
        await add_gallery_alias.finish(f"{alias} 已被画廊名称使用。")
    if alias in v.alias_to_name:
        await add_gallery_alias.finish(f"别名 {alias} 已被画廊 {v.alias_to_name[alias]} 使用。")

    # 添加别名
    v.alias_to_name[alias] = name
    v.name_to_aliases[name].append(alias)
    gallery_name_data.save_to_file()
    invalidate_gallery_render_cache()
    await add_gallery_alias.finish(f"成功为画廊 {name} 添加别名：{alias}")


@remove_gallery_alias.handle()
async def _(arg_msg: Message = CommandArg()):
    alias = arg_msg.extract_plain_text().strip()
    if not alias:
        await remove_gallery_alias.finish("请提供要删除的别名。")

    v = gallery_name_data.instance
    if alias not in v.alias_to_name:
        await remove_gallery_alias.finish(f"别名 {alias} 不存在。")

    # 删除别名
    name = v.alias_to_name.pop(alias)
    v.name_to_aliases[name].remove(alias)
    gallery_name_data.save_to_file()
    invalidate_gallery_render_cache()
    await remove_gallery_alias.finish(f"成功删除画廊 {name} 的别名：{alias}")


@gallery_pictures.handle()
async def _(arg_msg: Message = CommandArg()):
    name_or_alias = arg_msg.extract_plain_text().strip()
    if not name_or_alias:
        image = await asyncio.to_thread(render_gallery_overview)
        if not image:
            await gallery_pictures.finish("当前没有画廊。")
        await gallery_pictures.finish(OneBotMessageSegment.image(image))

    name = get_gallery_name(name_or_alias)
    if not name:
        await gallery_pictures.finish(f"未找到画廊：{name_or_alias}")

    gallery_dir = cfg.data_dir_path / name
    if not gallery_dir.is_dir():
        logger.warning(f"画廊索引中存在画廊名称 {name}，但对应的目录不存在：{gallery_dir}")
        await gallery_pictures.finish(f"画廊 {name} 的目录不存在。")

    pic_files = [path for path in gallery_dir.iterdir() if path.is_file()]
    if not pic_files:
        await gallery_pictures.finish(f"画廊 {name} 中没有图片。")

    image = await asyncio.to_thread(render_gallery_thumbnails, name, pic_files)
    if not image:
        await gallery_pictures.finish(f"画廊 {name} 中没有可读取的图片。")
    await gallery_pictures.finish(OneBotMessageSegment.image(image))


@get_picture.handle()
async def _(bot: OneBot, arg_msg: Message = CommandArg()):
    arg_str = arg_msg.extract_plain_text().strip()
    if not arg_str:
        await get_picture.finish("请提供画廊名称或图片id。")

    args = re.split(r"[x*×\s]+", arg_str, maxsplit=1)
    if not args or len(args) < 1:
        await get_picture.finish("请提供画廊名称。")
    arg1: str = args[0]

    name = get_gallery_name(arg1)
    if not name:
        if not arg1.isdigit():
            await get_picture.finish(f"未找到画廊：{arg1}")
        # 尝试按图片id获取图片
        if not (pic_file := get_picture_by_id(int(arg1))):
            await get_picture.finish(f"未找到图片id {arg1} 对应的图片。")
        if cfg.send_pic_as_meme:
            await get_picture.finish(OneBotMessageSegmentMeme(pic_file))
        else:
            await get_picture.finish(OneBotMessageSegment.image(pic_file))

    num = 1
    if len(args) > 1 and args[1].isdigit():
        num = int(args[1])
    gallery_dir = cfg.data_dir_path / name
    pic_files = list(gallery_dir.glob("*"))
    if not pic_files:
        await get_picture.finish(f"画廊 {name} 中没有图片。")

    if num < 1:
        await get_picture.finish("请提供有效的图片数量。")
    if num > cfg.send_pic_limit:
        await get_picture.finish(f"每次最多发送 {cfg.send_pic_limit} 张图片。")

    message = OneBotMessage()
    for _ in range(num):
        pic_file = random.choice(pic_files)
        if cfg.send_pic_as_meme:
            message += OneBotMessageSegmentMeme(pic_file)
        else:
            message += OneBotMessageSegment.image(pic_file)
    await get_picture.finish(message)


async def _get_image_from_url(url: str, file: str) -> Path | None:
    """从URL获取图片文件，返回图片文件路径"""
    r = await HTTPX_CLIENT.get(url)
    if r.status_code != 200:
        return None

    # 将图片保存到缓存目录
    cache_dir = get_plugin_config(KanadeConfig).image_cache_dir_path
    file_name = url.split("/")[-1]
    pic_path = cache_dir / file_name
    pic_path.write_bytes(r.content)
    return pic_path


async def _get_pictures_from_message(
    bot: OneBot,
    message: OneBotMessage,
    *,
    forward_image: bool = False,
) -> list[Path]:
    """从消息中提取图片文件

    :param forward_image: 当前message是否为转发消息
        如果是，则使用http client获取图片附件，否则使用bot.get_image获取图片附件
    """
    pictures: list[Path] = []
    for seg in message:
        if seg.type == "image":
            p: Path | None = None
            file: str = seg.data["file"]
            if forward_image:
                # 转发消息中的图片，直接使用http client获取图片附件
                p = await _get_image_from_url(seg.data["url"], file)
            else:
                # 普通消息中的图片，使用bot.get_image获取图片附件
                try:
                    p = await get_image_local(bot, file)
                except NetworkError as e:
                    logger.warning(f"bot.get_image获取图片附件失败: {file}, {e}")
                    # 回退到使用http client获取图片附件
                    p = await _get_image_from_url(seg.data["url"], file)
            if p:
                pictures.append(p)
            else:
                logger.warning(f"获取图片附件失败，消息段：{seg}")

        elif seg.type == "forward":
            _, fwd_msg_events = await get_forward_message_events(bot, seg)
            for e in fwd_msg_events:
                pictures.extend(
                    await _get_pictures_from_message(bot, e.message, forward_image=True)
                )
    return pictures


@add_picture.handle()
async def _(
    state: T_State,
    bot: OneBot,
    event: OneBotMessageEvent,
    arg_msg: Message = CommandArg(),
):
    args = parse_arg_message(
        arg_msg.extract_plain_text(),
        {"name_or_alias": str, "force": str},
        maxsplit=1,
    )
    name_or_alias: str | None = args["name_or_alias"]
    force_arg: str | None = args["force"]
    if not name_or_alias:
        await add_picture.finish("请提供画廊名称。")
    if force_arg is not None and force_arg.lower() != "force":
        await add_picture.finish("第二个参数仅支持 force，格式：<画廊名称> [force]")
    force = force_arg is not None
    name = get_gallery_name(name_or_alias)
    if not name:
        await add_picture.finish(f"未找到画廊：{name_or_alias}")

    # 获取引用的图片
    if event.reply:
        pic_paths = await _get_pictures_from_message(bot, event.reply.message)
        await _finish_add_pictures(name, pic_paths, force=force)

    # pause，要求用户发送图片
    state["gallery_name"] = name
    state["gallery_force"] = force
    await add_picture.pause(f"请发送要添加到画廊 {name} 的图片：")


@add_picture.handle()
async def _(state: T_State, bot: OneBot, message: OneBotMessage = EventMessage()):
    pic_paths = await _get_pictures_from_message(bot, message)
    name = state["gallery_name"]
    await _finish_add_pictures(name, pic_paths, force=state.get("gallery_force", False))


async def _finish_add_pictures(
    name: str,
    pic_paths: list[Path],
    *,
    force: bool,
) -> None:
    result = await asyncio.to_thread(add_pictures, name, pic_paths, force=force)
    response = OneBotMessage()
    if result.duplicate_image:
        response += OneBotMessageSegment.image(result.duplicate_image)
    response += OneBotMessageSegment.text(result.summary(name))
    await add_picture.finish(response)


@remove_picture.handle()
async def _(arg_msg: Message = CommandArg()):
    arg_str = arg_msg.extract_plain_text().strip()
    if not arg_str.isdigit():
        await remove_picture.finish("请提供有效的图片id。")

    pic_id = int(arg_str)
    pic_path = get_picture_by_id(pic_id)
    if pic_path is None:
        await remove_picture.finish(f"未找到图片id {pic_id} 对应的图片文件。")

    # 将图片文件移至废纸篓
    try:
        send2trash(pic_path)
    except OSError as e:
        logger.exception(f"删除图片文件 {pic_path} 失败：{e}")
        await remove_picture.finish(f"删除图片文件失败：{e}")
    remove_picture_from_index(pic_path)
    gallery_name = str(pic_path.parent.relative_to(cfg.data_dir_path))
    invalidate_gallery_render_cache()
    invalidate_gallery_render_cache(gallery_name)
    await remove_picture.finish(f"成功删除图片 {pic_id}。")
