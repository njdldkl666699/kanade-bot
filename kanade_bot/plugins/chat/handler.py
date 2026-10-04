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

from .agent.manager import chat_manager
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
    await chat_manager.reset_session(session_info.session_id)
    await chat_reset.finish("会话已重置")


@chat_interrupt.handle()
async def handle_chat_interrupt(event: Event):
    """手动中断当前正在进行的回复，等待中的消息不受影响照常处理"""
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await chat_manager.interrupt_session_turn(session_id)
    except Exception as e:  # noqa: BLE001
        logger.opt(exception=e).warning(f"中断会话{session_id}时发生错误")
        await chat_interrupt.finish(f"中断会话失败：{e}")
    if result is None:
        await chat_interrupt.finish("会话不存在（还未开始过对话），无需中断")
    await chat_interrupt.finish("已中断当前正在进行的回复，等待中的消息将照常处理")


# TODO 改为手动触发一次LLM会话压缩
@chat_compact.handle()
async def handle_chat_compact(event: Event):
    """查看会话存储统计：DB 全量保留，压缩后实际发送的条数更少

    注意：数据库为 append-only 全量保留，本命令**不删除任何消息**。
    """
    session_id = extract_session_info_sync(event).session_id
    try:
        result = await chat_manager.compact_session(session_id)
    except Exception as e:  # noqa: BLE001
        logger.opt(exception=e).warning(f"查询会话{session_id}存储时发生错误")
        await chat_compact.finish(f"查询会话存储失败（会话可能正在处理中）：{e}")
    if result is None:
        await chat_compact.finish("会话不存在（还未开始过对话），或尚无历史记录")

    await chat_compact.finish(
        "\n".join(
            [
                "会话存储统计（数据库全量保留，不做物理删除）",
                f"已存消息：{result['total']} 条",
                f"压缩后实际发送：{result['kept']} 条",
            ]
        )
    )


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
    await chat_manager.add_message(session_id, message_str)


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
        await chat_manager.add_system_notification(session_id, "表情包已更新")

    await add_meme.finish(f"已添加表情包 {name}")
