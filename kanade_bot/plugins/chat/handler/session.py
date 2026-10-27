"""会话管理命令"""

from datetime import datetime

from nonebot import logger
from nonebot.adapters import Event

from kanade_bot.utils.session import extract_session_info_sync

from ..agent.manager import chat_manager
from ..matcher import chat_compact, chat_interrupt, chat_reset, chat_stats, chat_workspace_clear


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
