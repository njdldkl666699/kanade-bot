import random
import re
from pathlib import Path

from nonebot import logger
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg, EventMessage
from nonebot.typing import T_State
from send2trash import send2trash

from kanade_bot.plugins.gallery.gallery import render_gallery_thumbnails
from kanade_bot.utils.parse import parse_arg_message

from .config import cfg, gallery_name_data
from .gallery import get_gallery_name, get_picture_by_id, save_pictures
from .matcher import (
    add_gallery,
    add_gallery_alias,
    add_picture,
    gallery_info,
    gallery_list,
    gallery_pictures,
    get_picture,
    remove_gallery,
    remove_gallery_alias,
    remove_picture,
)


@gallery_list.handle()
async def _(arg_msg: Message = CommandArg()):
    page_arg = arg_msg.extract_plain_text().strip()
    page: int = 1
    if page_arg.isdigit():
        page = int(page_arg)

    gallery_names = list(gallery_name_data.instance.name_to_aliases.keys())
    total = len(gallery_names)
    page_size = 10
    total_pages = (total + page_size - 1) // page_size

    if page < 1 or page > total_pages:
        await gallery_list.finish(f"无效的页码，请输入 1 到 {total_pages} 之间的数字。")

    messages = [f"画廊列表（共 {total} 个）"]
    start_index = (page - 1) * page_size
    end_index = min(start_index + page_size, total)
    for i in range(start_index, end_index):
        name = gallery_names[i]
        messages.append(f"{i + 1}. {name}")
    messages.append(f"第 {page}/{total_pages} 页")

    await gallery_list.finish("\n".join(messages))


@gallery_info.handle()
async def _(arg_msg: Message = CommandArg()):
    name_or_alias = arg_msg.extract_plain_text().strip()
    if not name_or_alias:
        await gallery_info.finish("请提供画廊名称或别名。")

    name = get_gallery_name(name_or_alias)
    if not name:
        await gallery_info.finish(f"未找到画廊：{name_or_alias}")

    # 获取画廊图片数量
    gallery_dir = cfg.data_dir_path / name
    if not gallery_dir.exists() or not gallery_dir.is_dir():
        logger.warning(f"画廊索引中存在画廊名称 {name}，但对应的目录不存在：{gallery_dir}")
        await gallery_info.finish(f"画廊 {name} 的目录不存在。")
    num_pictures = len(list(gallery_dir.glob("*")))

    # 获取别名列表
    aliases = gallery_name_data.instance.name_to_aliases.get(name, [])
    alias_str = ", ".join(aliases) if aliases else "无"
    # XXX: 这里可以考虑限制别名字符串的长度，避免过长的输出
    # alias_str_truncated = alias_str if len(alias_str) <= 50 else alias_str[:50] + "..."

    await gallery_info.finish(f"画廊：{name}\n图片数量：{num_pictures}\n别名：{alias_str}")


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
    await remove_gallery_alias.finish(f"成功删除画廊 {name} 的别名：{alias}")


@gallery_pictures.handle()
async def _(bot: OneBot, arg_msg: Message = CommandArg()):
    name_or_alias = arg_msg.extract_plain_text().strip()
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

    image = render_gallery_thumbnails(pic_files)
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
    arg1 = args[0]

    name = get_gallery_name(arg1)
    if not name:
        if not arg1.isdigit():
            await get_picture.finish(f"未找到画廊：{arg1}")
        # 尝试按图片id获取图片
        if pic_file := get_picture_by_id(int(arg1)):
            await get_picture.finish(OneBotMessageSegment.image(pic_file))
        else:
            await get_picture.finish(f"未找到图片id {arg1} 对应的图片。")

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
        message += OneBotMessageSegment.image(pic_file)
    await get_picture.finish(message)


@add_picture.handle()
async def _(
    state: T_State,
    bot: OneBot,
    event: OneBotMessageEvent,
    arg_msg: Message = CommandArg(),
):
    name_or_alias = arg_msg.extract_plain_text().strip()
    if not name_or_alias:
        await add_picture.finish("请提供画廊名称。")
    name = get_gallery_name(name_or_alias)
    if not name:
        await add_picture.finish(f"未找到画廊：{name_or_alias}")

    # 获取引用的图片
    if event.reply:
        pic_paths: list[Path] = []
        for seg in event.reply.message:
            if seg.type == "image":
                pic_url = seg.data["file"]
                r = await bot.get_image(file=pic_url)
                pic_paths.append(Path(r["file"]))

        save_pictures(name, pic_paths)
        await add_picture.finish(f"成功添加 {len(pic_paths)} 张图片到画廊 {name}。")

    # pause，要求用户发送图片
    state["gallery_name"] = name
    await add_picture.pause(f"请发送要添加到画廊 {name} 的图片：")


@add_picture.handle()
async def _(state: T_State, bot: OneBot, message: OneBotMessage = EventMessage()):
    pic_paths: list[Path] = []
    for seg in message:
        if seg.type == "image":
            pic_url = seg.data["file"]
            r = await bot.get_image(file=pic_url)
            pic_paths.append(Path(r["file"]))

    name = state["gallery_name"]
    save_pictures(name, pic_paths)
    await add_picture.finish(f"成功添加 {len(pic_paths)} 张图片到画廊 {name}。")


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
    await remove_picture.finish(f"成功删除图片 {pic_id}。")
