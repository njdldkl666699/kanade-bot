import uuid

from nonebot.adapters import Bot, Event, Message
from nonebot.adapters.console import Message as ConsoleMessage
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg, EventMessage

from kanade_bot.utils.parse import build_sender_info, parse_arg_message, parse_message_for_ai
from kanade_bot.utils.session import extract_session_info

from .agent.copilot import copilot
from .ban import add_to_ban_list, parse_ban_args, remove_from_ban_list
from .chat import send_message_in_chunks, should_auto_reply, should_reply_event
from .client import file_client as client
from .config import cfg, chat_configs, write_chat_config
from .matcher import add_meme, chat, chat_ban, chat_monitor, chat_reset, chat_unban, list_memes


@chat.handle()
async def handle_chat(
    bot: Bot,
    event: OneBotMessageEvent | ConsoleMessageEvent,
):
    # 检查用户或群聊是否在聊天黑名单中
    if should_reply_event(event):
        await send_message_in_chunks(chat, bot, event)


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    session_info = await extract_session_info(event)
    await copilot.reset_session(session_info.session_id)
    await chat_reset.finish("会话已重置")


@chat_monitor.handle()
async def handle_chat_monitor(
    bot: Bot,
    event: Event,
    message: ConsoleMessage | OneBotMessage = EventMessage(),
):
    session_info = await extract_session_info(event, bot)
    session_id = session_info.session_id

    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
        platform = "console"
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = str(event.group_id)
        platform = "onebot"
    else:
        return

    if session_info.group_name and should_auto_reply(group_id, platform, session_id):
        await send_message_in_chunks(chat, bot, event)

    # 添加消息到会话缓冲区，不需要图片
    message_str, _ = await parse_message_for_ai(message)
    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        message_str = f"{user_info} : {message_str}"
    await copilot.add_message(session_id, message_str)


@chat_ban.handle()
async def handle_chat_ban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_ban.finish()
    id, ban_type = args

    if isinstance(event, ConsoleMessageEvent):
        add_to_ban_list(id, ban_type, "console")
    elif isinstance(event, OneBotMessageEvent):
        add_to_ban_list(id, ban_type, "onebot")
    else:
        await chat_ban.finish()

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_ban.finish(f"已将{type_text} {id} 添加到聊天黑名单")


@chat_unban.handle()
async def handle_chat_unban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_unban.finish()
    id, ban_type = args

    if isinstance(event, ConsoleMessageEvent):
        remove_from_ban_list(id, ban_type, "console")
    elif isinstance(event, OneBotMessageEvent):
        remove_from_ban_list(id, ban_type, "onebot")
    else:
        await chat_unban.finish()

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_unban.finish(f"已将{type_text} {id} 从聊天黑名单中移除")


@list_memes.handle()
async def handle_list_memes():
    if not chat_configs.memes:
        await list_memes.finish("当前没有表情包")

    meme_list = "\n".join(
        f"{name}: {description}" for name, description in chat_configs.memes.items()
    )
    await list_memes.finish(f"当前表情包列表：\n{meme_list}")


@add_meme.handle()
async def handle_add_meme(event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg.extract_plain_text(), {"name": str, "description": str})
    name: str | None = args.get("name") or None
    if not name:
        await add_meme.finish("请输入表情包名称")
    description = args.get("description") or None

    # 获取引用图片的第一张
    if not event.reply:
        await add_meme.finish()
    message = event.reply.message
    image_url: str | None = None
    for segment in message:
        if segment.type == "image":
            image_url = segment.data.get("url")
            break
    if not image_url:
        await add_meme.finish()

    # 下载图片
    response = await client.get(image_url)
    response.raise_for_status()
    image = response.content
    # 确保表情包目录存在
    meme_path = cfg.memes_dir_path / name
    meme_path.mkdir(parents=True, exist_ok=True)
    # 保存图片到表情包目录
    image_path = meme_path / f"{uuid.uuid4()}.png"
    image_path.write_bytes(image)

    # 将表情包信息添加（或更新）到配置中
    memes = chat_configs.memes
    # 如果表情包名称不存在，或新描述不为空，则更新配置文件
    if name not in memes or description:
        memes[name] = description
        write_chat_config()
    await add_meme.finish(f"已添加表情包 {name}")
