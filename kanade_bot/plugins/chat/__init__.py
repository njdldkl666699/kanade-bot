import uuid
from pathlib import Path

from nonebot import get_driver, get_plugin_config, on_command, on_message
from nonebot.adapters import Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg, EventPlainText, EventToMe
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from kanade_bot.plugins.util import parse_arg_message

from .ban import add_to_ban_list, is_event_banned, remove_from_ban_list
from .client import file_client as client
from .config import Config, configs, write_chat_config
from .copilot import copilot
from .util import resolve_session_id_and_prompt, send_message_in_chunks, should_auto_reply

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)

### 聊天命令
chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


@chat.handle()
async def handle_chat(event: Event, prompt: str = EventPlainText()):
    # 检查用户或群聊是否在聊天黑名单中
    if is_event_banned(event):
        return

    # 解析会话ID和提示词
    session_id, prompt, is_group = resolve_session_id_and_prompt(event, prompt)
    await send_message_in_chunks(chat, event, session_id, prompt, is_group)


def not_to_me(to_me: bool = EventToMe()):
    return not to_me


### 聊天监听命令
# 用于监听非@消息并将其添加到会话缓冲区，以便在下一次@消息时一起发送给模型
chat_monitor = on_message(
    rule=not_to_me,
    priority=3,
    block=False,
)


@chat_monitor.handle()
async def handle_chat_monitor(event: Event, prompt: str = EventPlainText()):
    session_id, prompt, is_group = resolve_session_id_and_prompt(event, prompt)

    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
        platform = "console"
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = str(event.group_id)
        platform = "onebot"
    else:
        return

    if is_group and should_auto_reply(group_id, platform, session_id):
        await send_message_in_chunks(chat, event, session_id, prompt, is_group)

    # 将用户消息添加到会话缓冲区
    await copilot.add_message(session_id, prompt)


global_config = get_driver().config

### 重置会话命令
chat_reset = on_command(
    "重置会话",
    aliases={"chat_reset", "chatreset", "重置对话"},
    priority=2,
    block=True,
)


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    admin_operates = False
    admin_id = event.get_user_id()
    if admin_id in global_config.superusers:
        admin_operates = True
    if isinstance(event, ConsoleMessageEvent):
        admin_operates = True
    if not admin_operates:
        await chat_reset.finish()

    session_id, _, _ = resolve_session_id_and_prompt(event, "")
    await copilot.reset_session(session_id)
    await chat_reset.finish("会话已重置")


### 聊天黑名单
chat_ban = on_command(
    "聊天拉黑",
    aliases={"chat_ban", "chatban"},
    priority=2,
    block=True,
)


@chat_ban.handle()
async def handle_chat_ban(event: Event, arg_msg: Message = CommandArg()):
    admin_id = event.get_user_id()
    if admin_id not in global_config.superusers:
        await chat_ban.finish()

    args = parse_arg_message(arg_msg, {"id": str, "ban_type": str})
    id = args.get("id", "").strip()
    ban_type = args.get("ban_type", "user").strip().lower()

    if isinstance(event, ConsoleMessageEvent):
        add_to_ban_list(id, ban_type, "console")
    elif isinstance(event, OneBotMessageEvent):
        add_to_ban_list(id, ban_type, "onebot")
    else:
        await chat_ban.finish()

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_ban.finish(f"已将{type_text} {id} 添加到聊天黑名单")


chat_unban = on_command(
    "聊天解除拉黑",
    aliases={"chat_unban", "chatunban"},
    priority=2,
    block=True,
)


@chat_unban.handle()
async def handle_chat_unban(event: Event, arg_msg: Message = CommandArg()):
    admin_id = event.get_user_id()
    if admin_id not in global_config.superusers:
        await chat_unban.finish()

    args = parse_arg_message(arg_msg, {"id": str, "ban_type": str})
    id = args.get("id", "").strip()
    ban_type = args.get("ban_type", "user").strip().lower()

    if isinstance(event, ConsoleMessageEvent):
        remove_from_ban_list(id, ban_type, "console")
    elif isinstance(event, OneBotMessageEvent):
        remove_from_ban_list(id, ban_type, "onebot")
    else:
        await chat_unban.finish()

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_unban.finish(f"已将{type_text} {id} 从聊天黑名单中移除")


### 表情相关命令
list_memes = on_command(
    "表情包列表",
    aliases={"list_memes", "listmemes"},
    priority=2,
    block=True,
)


@list_memes.handle()
async def handle_list_memes():
    if not configs.memes:
        await list_memes.finish("当前没有表情包")

    meme_list = "\n".join(f"{name}: {description}" for name, description in configs.memes.items())
    await list_memes.finish(f"当前表情包列表：\n{meme_list}")


add_meme = on_command(
    "添加表情",
    aliases={"add_meme", "addmeme"},
    priority=2,
    block=True,
)


@add_meme.handle()
async def handle_add_meme(event: OneBotMessageEvent, arg_msg: Message = CommandArg()):
    if event.get_user_id() not in global_config.superusers:
        await add_meme.finish()

    args = parse_arg_message(arg_msg, {"name": str, "description": str})
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
    meme_path = Path(cfg.chat_memes_path) / name
    meme_path.mkdir(parents=True, exist_ok=True)
    # 保存图片到表情包目录
    image_path = meme_path / f"{uuid.uuid4()}.png"
    image_path.write_bytes(image)

    # 将表情包信息添加（或更新）到配置中
    memes = configs.memes
    # 如果表情包名称不存在，或新描述不为空，则更新配置文件
    if name not in memes or description:
        memes[name] = description
        write_chat_config()
    await add_meme.finish(f"已添加表情包 {name}")
