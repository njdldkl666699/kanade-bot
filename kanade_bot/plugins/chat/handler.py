from datetime import datetime
from pathlib import Path

import magic
from nonebot import logger, require
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg

from kanade_bot.utils.common import asia_shanghai_now_str, get_platform_type
from kanade_bot.utils.onebot11 import get_image_path
from kanade_bot.utils.parse import build_sender_info, parse_message_for_ai
from kanade_bot.utils.session import extract_session_info, extract_session_info_sync

from .agent.auto_reply import auto_reply_runtime, set_group_enabled
from .agent.manager import chat_manager
from .agent.schedule import scheduled_task_manager
from .agent.vision import get_image_tags
from .ban import add_to_ban_list, parse_ban_args, remove_from_ban_list
from .chat import send_message_in_chunks, should_reply_event
from .config import cfg
from .matcher import (
    add_meme,
    auto_reply_disable,
    auto_reply_enable,
    chat,
    chat_ban,
    chat_compact,
    chat_interrupt,
    chat_monitor,
    chat_reset,
    chat_stats,
    chat_task_cancel,
    chat_task_list,
    chat_unban,
    chat_workspace_clear,
    decision_clear,
    decision_stats,
    del_meme,
    list_memes,
    retag_meme,
    scan_memes,
)
from .meme_index import dedupe_tags, parse_tags
from .meme_service import get_memes

require("crystal")
from kanade_bot.plugins.crystal import HandlerKeyEnum, check_user_crystal, finish_fail_consume


def _group_id_of(event: Event) -> str | None:
    """从群消息事件提取群 ID；非群消息返回 None"""
    if isinstance(event, ConsolePublicMessageEvent):
        return event.channel.id
    if isinstance(event, OneBotGroupMessageEvent):
        return str(event.group_id)
    return None


@chat.handle()
async def handle_chat(bot: Bot, event: OneBotMessageEvent | ConsoleMessageEvent):
    if not should_reply_event(event):
        return

    key = HandlerKeyEnum.CHAT
    platform = get_platform_type(event)
    user_id = event.get_user_id()
    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(chat, key, platform, user_id)

    await send_message_in_chunks(chat, bot, event)


@auto_reply_enable.handle()
async def handle_auto_reply_enable(event: Event):
    """在当前群启用主动回复（SUPERUSER）"""
    if (group_id := _group_id_of(event)) is None:
        await auto_reply_enable.finish("请在群聊中使用本命令")
    set_group_enabled(group_id, get_platform_type(event), True)
    message = f"已启用群 {group_id} 的主动回复"
    if not cfg.auto_reply.enabled:
        message += "\n注意：全局开关 chat.auto_reply.enabled 未开启，请先在配置文件中启用"
    await auto_reply_enable.finish(message)


@auto_reply_disable.handle()
async def handle_auto_reply_disable(event: Event):
    """在当前群禁用主动回复（SUPERUSER）"""
    if (group_id := _group_id_of(event)) is None:
        await auto_reply_disable.finish("请在群聊中使用本命令")
    set_group_enabled(group_id, get_platform_type(event), False)
    await auto_reply_disable.finish(f"已禁用群 {group_id} 的主动回复")


@chat_monitor.handle()
async def handle_chat_monitor(bot: Bot, event: Event):
    session_info = await extract_session_info(event, bot)
    session_id = session_info.session_id
    platform = get_platform_type(event)

    group_id = _group_id_of(event)

    # 添加消息到会话缓冲区，不需要图片
    message_str, _ = await parse_message_for_ai(event)
    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        message_str = f"[{asia_shanghai_now_str('%Y-%m-%d %H:%M')}] {user_info}：{message_str}"
    await chat_manager.get_session(session_id).add_message(message_str)

    # 主动回复流水线：消息 append 决策历史 + 防抖观察
    if group_id and session_info.group_name and auto_reply_runtime.is_enabled(group_id, platform):
        await auto_reply_runtime.on_group_message(
            session_id,
            group_id,
            message_str,
            author=session_info.user_id,
            bot=bot,
            session_info=session_info,
        )


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    session_info = extract_session_info_sync(event)
    await chat_manager.get_session(session_info.session_id).reset()
    await chat_reset.finish("会话已重置")


@chat_workspace_clear.handle()
async def handle_chat_workspace_clear(event: Event):
    """删除当前会话的沙箱工作区文件，不影响会话历史"""
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await chat_manager.get_session(session_id).clear_workspace()
    except Exception as e:
        logger.opt(exception=e).warning(f"清理会话{session_id}工作区时发生错误")
        await chat_workspace_clear.finish(f"清理工作区失败：{e}")
    if result is None:
        await chat_workspace_clear.finish("沙箱未启用，无工作区")
    if not result:
        await chat_workspace_clear.finish("工作区不存在（尚未创建或已清理）")
    await chat_workspace_clear.finish("工作区已清理")


@chat_interrupt.handle()
async def handle_chat_interrupt(event: Event):
    """手动中断当前正在进行的回复，等待中的消息不受影响照常处理"""
    session_id = extract_session_info_sync(event).session_id
    try:
        result = chat_manager.get_session(session_id).interrupt()
    except Exception as e:
        logger.opt(exception=e).warning(f"中断会话{session_id}时发生错误")
        await chat_interrupt.finish(f"中断会话失败：{e}")
    if result is None:
        await chat_interrupt.finish("会话不存在（还未开始过对话），无需中断")
    await chat_interrupt.finish("已中断当前正在进行的回复，等待中的消息将照常处理")


@chat_compact.handle()
async def handle_chat_compact(event: Event):
    """手动执行一次 LLM 总结级会话压缩

    不论是否达到自动触发阈值，立即把当前会话的早期历史压缩为一条摘要
    """
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await chat_manager.get_session(session_id).compact()
    except Exception as e:
        logger.opt(exception=e).warning(f"手动压缩会话{session_id}时发生错误")
        await chat_compact.finish(f"会话压缩失败：{e}")
    if result is None:
        await chat_compact.finish("会话不存在（还未开始过对话），或尚无历史记录")

    if not result["compacted"]:
        await chat_compact.finish(
            f"无可压缩内容：历史 {result['before']} 条均在保留尾部内，无更早消息可摘要"
        )

    lines = [
        "✅ 会话压缩完成（LLM 摘要）",
        f"发送历史：{result['before']} 条 → {result['after']} 条",
        f"上下文估算：{result['tokens_before']} → {result['tokens_after']} tokens",
    ]
    if summary := result.get("summary"):
        preview = summary if len(summary) <= 300 else summary[:300] + "…"
        lines.append(f"摘要预览：\n{preview}")
    lines.append(f"数据库全量保留 {result['total']} 条，不做物理删除")
    await chat_compact.finish("\n".join(lines))


@chat_stats.handle()
async def handle_chat_stats(event: Event):
    """查看会话统计：上下文token估算/模型窗口上限、消息条数、缓存命中率、工作区文件列表"""
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await chat_manager.get_session(session_id).stats()
    except Exception as e:
        logger.opt(exception=e).warning(f"查询会话{session_id}统计时发生错误")
        await chat_stats.finish(f"查询会话统计失败（会话可能正在处理中）：{e}")
    if result is None:
        await chat_stats.finish("会话不存在（还未开始过对话），或尚无历史记录")

    window = result["context_window"]
    tokens = result["context_tokens"]
    window_line = f"{window} tokens" if window else "未知"
    usage_line = f"{tokens} tokens"
    if window:
        usage_line += f"（{tokens / window:.1%}）"

    emptied = result.get("emptied_messages", 0)
    message_line = (
        f"消息：数据库全量 {result['total_messages']} 条，"
        f"压缩后发送 {result['sent_messages'] - emptied} 条"
    )

    lines = [
        "📊 会话统计",
        f"模型：{result['model']} | 窗口上限：{window_line}",
        f"上下文估算：{usage_line}",
        message_line,
    ]

    if cache := result.get("cache"):
        lines.append(
            f"平均缓存命中：{cache['hit_rate']:.1%} {cache['cache_read_tokens']}/{cache['input_tokens']} tokens"
        )

    if comp := result.get("compaction"):
        label = {
            "clear_tool_results": "清除工具结果",
            "summarizing": "摘要压缩",
        }.get(comp["latest_strategy"], comp["latest_strategy"])
        latest_at = datetime.fromtimestamp(comp["latest_at"]).strftime("%m-%d %H:%M")
        lines.append(f"压缩：已应用 {comp['marks']} 次，最近 {label} @ {latest_at}")

    if files := result.get("workspace_files"):
        lines.append("工作区文件：")
        lines.extend(files)
    else:
        lines.append("工作区：未启用沙箱或工作区为空")

    await chat_stats.finish("\n".join(lines))


@decision_stats.handle()
async def handle_decision_stats(event: Event):
    """输出当前会话的决策器统计"""
    session_info = extract_session_info_sync(event)
    group_label = session_info.group_id or session_info.session_id
    text = await auto_reply_runtime.format_stats(session_info.session_id, group_label)
    if text is None:
        await decision_stats.finish("当前会话未进入主动回复流水线（未启用或启动后尚无消息观察）")
    await decision_stats.finish(text)


@decision_clear.handle()
async def handle_decision_clear(event: Event):
    """清空当前会话的决策器上下文（SUPERUSER）"""
    session_id = extract_session_info_sync(event).session_id
    deleted = await auto_reply_runtime.clear_history(session_id)
    if deleted is None:
        await decision_clear.finish("当前会话未进入主动回复流水线（未启用或启动后尚无消息观察）")
    await decision_clear.finish(
        f"已清空决策器上下文（删除 {deleted} 条历史记录）\n"
        "决策器将从零开始重新旁观；统计计数与防抖/退避状态保留，主会话不受影响"
    )


@chat_task_list.handle()
async def handle_chat_task_list(event: Event):
    """列出当前会话的Agent定时任务"""
    session_id = extract_session_info_sync(event).session_id
    tasks = scheduled_task_manager.list_by_session(session_id)
    if not tasks:
        await chat_task_list.finish("当前会话没有定时任务")

    lines = [f"📋 当前会话的定时任务（{len(tasks)}）："]
    for task in tasks:
        description = (
            task.description if len(task.description) <= 40 else task.description[:40] + "…"
        )
        creator = f"（by {task.creator_name}）" if task.creator_name else ""
        lines.append(f"- [{task.task_id}] {task.run_at_dt:%m-%d %H:%M} {description}{creator}")
    await chat_task_list.finish("\n".join(lines))


@chat_task_cancel.handle()
async def handle_chat_task_cancel(event: Event, arg_msg: Message = CommandArg()):
    """按ID取消当前会话创建的定时任务"""
    task_id = arg_msg.extract_plain_text().strip()
    if not task_id:
        await chat_task_cancel.finish("请提供要取消的任务ID，可用「任务列表」查询")

    session_id = extract_session_info_sync(event).session_id
    if await scheduled_task_manager.cancel(task_id, session_id=session_id):
        await chat_task_cancel.finish(f"已取消定时任务 {task_id}")
    await chat_task_cancel.finish(f"当前会话不存在任务 {task_id}（可用「任务列表」查询）")


@chat_ban.handle()
async def handle_chat_ban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_ban.finish()
    id, ban_type = args

    platform = get_platform_type(event)
    add_to_ban_list(id, ban_type, platform)

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_ban.finish(f"已将{type_text} {id} 添加到聊天黑名单")


@chat_unban.handle()
async def handle_chat_unban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_unban.finish()
    id, ban_type = args

    platform = get_platform_type(event)
    remove_from_ban_list(id, ban_type, platform)

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_unban.finish(f"已将{type_text} {id} 从聊天黑名单中移除")


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
