import shutil
import uuid
from pathlib import Path

from nonebot import logger, require
from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg

from kanade_bot.utils.common import get_platform_type
from kanade_bot.utils.onebot11 import get_image_path
from kanade_bot.utils.parse import build_sender_info, parse_arg_message, parse_message_for_ai
from kanade_bot.utils.session import extract_session_info, extract_session_info_sync

from .agent.copilot import copilot
from .ban import add_to_ban_list, parse_ban_args, remove_from_ban_list
from .chat import send_message_in_chunks, should_auto_reply, should_reply_event
from .config import cfg, chat_configs
from .matcher import (
    add_meme,
    chat,
    chat_ban,
    chat_compact,
    chat_interrupt,
    chat_monitor,
    chat_reset,
    chat_unban,
    list_memes,
)

require("crystal")

from kanade_bot.plugins.crystal import HandlerKeyEnum, check_user_crystal, finish_fail_consume


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


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    session_info = extract_session_info_sync(event)
    await copilot.reset_session(session_info.session_id)
    await chat_reset.finish("会话已重置")


@chat_interrupt.handle()
async def handle_chat_interrupt(event: Event):
    """手动中断当前正在进行的回复，等待中的消息不受影响照常处理"""
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await copilot.interrupt_session_turn(session_id)
    except Exception as e:  # noqa: BLE001
        logger.opt(exception=e).warning(f"中断会话{session_id}时发生错误")
        await chat_interrupt.finish(f"中断会话失败：{e}")
    if result is None:
        await chat_interrupt.finish("会话不存在（还未开始过对话），无需中断")
    if not result.interrupted:
        await chat_interrupt.finish("当前没有正在进行的回复")
    await chat_interrupt.finish("已中断当前正在进行的回复，等待中的消息将照常处理")


@chat_compact.handle()
async def handle_chat_compact(event: Event):
    """手动压缩会话历史，返回压缩前后的上下文对比"""
    session_id = extract_session_info_sync(event).session_id
    await chat_compact.send("正在压缩会话历史，可能需要一会儿…")
    try:
        result = await copilot.compact_session(session_id)
    except Exception as e:  # noqa: BLE001
        if "Nothing to compact" in str(e):
            await chat_compact.finish("没有可压缩的内容（会话历史太短）")
        logger.opt(exception=e).warning(f"压缩会话{session_id}时发生错误")
        await chat_compact.finish(f"压缩会话失败（会话可能正在处理中）：{e}")
    if result is None:
        await chat_compact.finish("会话不存在（还未开始过对话），无需压缩")

    lines = [f"压缩{'完成' if result.success else '未成功'}"]
    if result.tokens_removed >= 0:
        lines.append(f"移除：{result.messages_removed}条消息 / {result.tokens_removed} tokens")
    else:
        lines.append(
            f"移除：{result.messages_removed}条消息（tokens净增{-result.tokens_removed}，摘要比原文更长）"
        )
    if cw := result.context_window:
        before_tokens = cw.current_tokens + result.tokens_removed
        before_msgs = cw.messages_length + result.messages_removed
        usage = cw.current_tokens / cw.token_limit if cw.token_limit else 0
        lines.append(f"消息数：{before_msgs} → {cw.messages_length}")
        lines.append(
            f"上下文tokens：{before_tokens} → {cw.current_tokens}（上限{cw.token_limit}，当前{usage:.0%}）"
        )
        detail = []
        if cw.conversation_tokens is not None:
            detail.append(f"对话{cw.conversation_tokens}")
        if cw.system_tokens is not None:
            detail.append(f"系统{cw.system_tokens}")
        if cw.tool_definitions_tokens is not None:
            detail.append(f"工具定义{cw.tool_definitions_tokens}")
        if detail:
            lines.append(f"压缩后构成：{'、'.join(detail)}")
    if summary := (result.summary_content or "").strip():
        if len(summary) > 300:
            summary = summary[:300] + "…"
        lines.append(f"摘要预览：\n{summary}")
    await chat_compact.finish("\n".join(lines))


@chat_monitor.handle()
async def handle_chat_monitor(bot: Bot, event: Event):
    session_info = await extract_session_info(event, bot)
    session_id = session_info.session_id
    platform = get_platform_type(event)

    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = str(event.group_id)
    else:
        return

    if session_info.group_name and should_auto_reply(group_id, platform, session_id):
        await send_message_in_chunks(chat, bot, event, auto_reply=True)

    # 添加消息到会话缓冲区，不需要图片
    message_str, _ = await parse_message_for_ai(event)
    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        message_str = f"{user_info}：{message_str}"
    await copilot.add_message(session_id, message_str)


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
    if not chat_configs.instance.memes:
        await list_memes.finish("当前没有表情包")

    meme_list = "\n".join(
        f"{name}: {description}" for name, description in chat_configs.instance.memes.items()
    )
    await list_memes.finish(f"当前表情包列表：\n{meme_list}")


@add_meme.handle()
async def handle_add_meme(bot: OneBot, event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"name": str, "description": str})
    name: str | None = args.get("name") or None
    if not name:
        await add_meme.finish("请输入表情包名称")
    description = args.get("description") or None

    # 获取引用图片的第一张
    if not event.reply:
        await add_meme.finish()

    local_path: Path | None = None
    for segment in event.reply.message:
        if segment.type == "image":
            local_path = await get_image_path(bot, segment)
            break
    if not local_path:
        await add_meme.finish()

    # 确保表情包目录存在
    meme_path = cfg.memes_dir_path / name
    meme_path.mkdir(parents=True, exist_ok=True)
    # 保存图片到表情包目录
    image_path = meme_path / f"{uuid.uuid4()}.png"
    shutil.copy(local_path, image_path)

    # 将表情包信息添加（或更新）到配置中
    memes = chat_configs.instance.memes
    # 如果表情包名称不存在，或新描述不为空，则更新配置文件
    if name not in memes or description:
        memes[name] = description
        # 添加系统通知
        session_info = await extract_session_info(event, bot)
        session_id = session_info.session_id
        await copilot.add_system_notification(session_id, "表情包已更新")

    await add_meme.finish(f"已添加表情包 {name}")
