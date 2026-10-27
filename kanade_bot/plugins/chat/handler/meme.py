"""表情包管理命令"""

from pathlib import Path

import magic
from nonebot.adapters import Message
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg

from kanade_bot.utils.onebot11 import get_image_path
from kanade_bot.utils.session import extract_session_info

from ..agent.manager import chat_manager
from ..matcher import add_meme, del_meme, list_memes, retag_meme, scan_memes
from ..memes import get_memes
from ..memes.index import dedupe_tags, parse_tags
from ..memes.tagging import get_image_tags


@list_memes.handle()
async def handle_list_memes():
    memes = get_memes()
    if memes is None:
        await list_memes.finish("表情包功能未启用")
    if not memes.records:
        await list_memes.finish("当前没有表情包")

    lines = []
    for r in memes.records:
        tag_text = " ".join(r.tags) if r.tags else "（待打标）"
        lines.append(f"#{r.id} {r.file}: {tag_text}")
    await list_memes.finish("当前表情包列表：\n" + "\n".join(lines))


async def _get_reply_image(bot: OneBot, event: OneBotMessageEvent) -> Path | None:
    """取引用消息中的第一张图片的本地缓存路径"""
    if not event.reply:
        return None
    for segment in event.reply.message:
        if segment.type == "image":
            return await get_image_path(bot, segment)
    return None


@add_meme.handle()
async def handle_add_meme(bot: OneBot, event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    memes = get_memes()
    if memes is None:
        await add_meme.finish("表情包功能未启用")

    local_path = await _get_reply_image(bot, event)
    if not local_path:
        await add_meme.finish("请引用一张图片使用此命令")

    # 命令参数作为人工补充标签，与视觉打标结果合并
    extra_tags = parse_tags(arg_msg.extract_plain_text())
    data = local_path.read_bytes()
    mime_type = magic.from_buffer(data[:64], mime=True)

    await add_meme.send("正在识别表情包…")
    tags = await get_image_tags(data, mime_type)
    if tags is None:
        rec = await memes.add_image(local_path, extra_tags)
        if extra_tags:
            await add_meme.finish(f"打标失败，已用补充标签收录为 #{rec.id}")
        await add_meme.finish(f"打标失败，已收录为 #{rec.id}（待打标），请用「改标」命令补充标签")

    rec = await memes.add_image(local_path, dedupe_tags(tags + extra_tags))
    # 通知会话内 Agent 新表情包已收录
    session_info = await extract_session_info(event, bot)
    await chat_manager.get_session(session_info.session_id).add_system_notification(
        f"新表情包已收录：{' '.join(rec.tags)}"
    )
    await add_meme.finish(f"已收录表情包 #{rec.id}，标签：{' '.join(rec.tags)}")


@retag_meme.handle()
async def handle_retag_meme(arg_msg: Message = CommandArg()):
    memes = get_memes()
    if memes is None:
        await retag_meme.finish("表情包功能未启用")

    parts = arg_msg.extract_plain_text().split(maxsplit=1)
    if len(parts) < 2 or not parts[0].strip().lstrip("#").isdigit():
        await retag_meme.finish(
            "用法：改标 <序号> <标签…>（序号见「表情包列表」，空格分隔多个标签）"
        )

    rec_id = int(parts[0].strip().lstrip("#"))
    tags = parse_tags(parts[1])
    if not tags:
        await retag_meme.finish("标签不能为空")

    rec = await memes.retag(rec_id, tags)
    if rec is None:
        await retag_meme.finish(f"不存在 #{rec_id}，请用「表情包列表」查询序号")
    await retag_meme.finish(f"已更新 #{rec_id} 标签：{' '.join(rec.tags)}")


@del_meme.handle()
async def handle_del_meme(arg_msg: Message = CommandArg()):
    memes = get_memes()
    if memes is None:
        await del_meme.finish("表情包功能未启用")

    arg = arg_msg.extract_plain_text().strip().lstrip("#")
    if not arg.isdigit():
        await del_meme.finish("用法：删除表情 <序号>（序号见「表情包列表」）")

    if memes.remove(int(arg)):
        await del_meme.finish(f"已删除表情包 #{arg}")
    await del_meme.finish(f"不存在 #{arg}，请用「表情包列表」查询序号")


@scan_memes.handle()
async def handle_scan_memes():
    memes = get_memes()
    if memes is None:
        await scan_memes.finish("表情包功能未启用")

    # 清理悬空记录（索引中的文件已被手动删除）
    dangling = memes.dangling_records()
    for r in dangling:
        memes.remove(r.id)

    pending = memes.unindexed_files()
    if not pending:
        summary = f"没有需要收录的表情包（索引共 {len(memes.records)} 条）"
        if dangling:
            summary += f"，已清理悬空记录 {len(dangling)} 条"
        await scan_memes.finish(summary)

    await scan_memes.send(f"发现 {len(pending)} 张未收录图片，开始打标…")
    tagged = 0
    failed = 0
    for path in pending:
        data = path.read_bytes()
        mime_type = magic.from_buffer(data[:64], mime=True)
        tags = await get_image_tags(data, mime_type)
        try:
            await memes.adopt_file(path, tags or [])
        except ValueError:
            continue
        if tags:
            tagged += 1
        else:
            failed += 1

    summary = f"扫描完成：新收录 {tagged} 张"
    if failed:
        summary += f"（{failed} 张打标失败待「改标」补标）"
    if dangling:
        summary += f"，清理悬空记录 {len(dangling)} 条"
    await scan_memes.finish(summary)
