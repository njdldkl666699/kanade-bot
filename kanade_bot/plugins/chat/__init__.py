import re

from nonebot import get_driver, get_plugin_config, on_command, on_message
from nonebot.adapters import Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg, EventPlainText, EventToMe
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from kanade_bot.plugins.chat.ban import (
    add_user_to_ban_list,
    is_user_banned,
    remove_user_from_ban_list,
)

from .config import Config
from .copilot import copilot

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


def resolve_session_id_and_prompt(event: Event, prompt: str) -> tuple[str, str, bool]:
    """解析事件以获取会话ID和提示词，并返回是否是群聊"""
    session_id = event.get_session_id()
    nickname: str | None = None
    is_group = False

    # 处理OneBot消息事件
    if isinstance(event, OneBotMessageEvent):
        nickname = event.sender.nickname
    if isinstance(event, OneBotGroupMessageEvent):
        session_id = str(event.group_id)
        nickname = event.sender.card or event.sender.nickname
        is_group = True

    # Console的消息事件
    if isinstance(event, ConsoleMessageEvent):
        nickname = event.user.nickname
    if isinstance(event, ConsolePublicMessageEvent):
        session_id = event.channel.id
        is_group = True

    prompt = f"{nickname}说：{prompt}" if nickname else prompt
    return session_id, prompt, is_group


### 聊天命令
chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


@chat.handle()
async def handle_chat(event: Event, prompt: str = EventPlainText()):
    # 检查用户是否在聊天黑名单中
    user_id = event.get_user_id()
    if isinstance(event, ConsoleMessageEvent) and is_user_banned(user_id, "console"):
        await chat.finish()
    if isinstance(event, OneBotMessageEvent) and is_user_banned(user_id, "onebot"):
        await chat.finish()

    # 解析会话ID和提示词
    session_id, prompt, is_group = resolve_session_id_and_prompt(event, prompt)
    response, new_session = await copilot.send_and_wait(
        session_id,
        prompt,
        timeout=300,
        is_group=is_group,
    )

    if new_session:
        await chat.send("会话过期，开启了新会话")
    if response and response.data.content:
        content = response.data.content
        # 按两个及以上换行拆分，单个换行保持原样
        chunks = [chunk for chunk in re.split(r"(?:\r?\n){2,}", content) if chunk.strip()]
        # 消息数<=3，按条发送
        if len(chunks) <= 3:
            for chunk in chunks:
                await chat.send(chunk)
            await chat.finish()
        # 消息数>3
        # Console合并成一条发送
        if isinstance(event, ConsoleMessageEvent):
            await chat.finish(content)
        # OneBot分成多条，合并转发
        if isinstance(event, OneBotMessageEvent):
            message = OneBotMessage()
            for chunk in chunks:
                message += MessageSegment.node_custom(cfg.chat_bot_id, cfg.chat_bot_nickname, chunk)
            await chat.finish(message)
        await chat.finish()
    else:
        await chat.finish("模型未响应，请稍后再试")


def not_to_me(to_me: bool = EventToMe()):
    return not to_me


### 聊天监听命令
# 用于监听非@消息并将其添加到会话缓冲区，以便在下一次@消息时一起发送给模型
chat_monitor = on_message(
    rule=not_to_me,
    priority=1,
    block=False,
)


@chat_monitor.handle()
async def handle_chat_monitor(event: Event, prompt: str = EventPlainText()):
    session_id, prompt, _ = resolve_session_id_and_prompt(event, prompt)

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
    admin_id = event.get_user_id()
    if admin_id not in global_config.superusers:
        await chat_reset.finish()

    session_id, _, _ = resolve_session_id_and_prompt(event, "")
    await copilot.clear_session(session_id)
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

    user_id = arg_msg.extract_plain_text().strip()
    if not user_id:
        await chat_ban.finish()

    if isinstance(event, ConsoleMessageEvent):
        add_user_to_ban_list(user_id, "console")
    elif isinstance(event, OneBotMessageEvent):
        add_user_to_ban_list(user_id, "onebot")
    else:
        await chat_ban.finish()

    await chat_ban.finish(f"已将用户 {user_id} 添加到聊天黑名单")


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

    user_id = arg_msg.extract_plain_text().strip()
    if not user_id:
        await chat_unban.finish()

    if isinstance(event, ConsoleMessageEvent):
        remove_user_from_ban_list(user_id, "console")
    elif isinstance(event, OneBotMessageEvent):
        remove_user_from_ban_list(user_id, "onebot")
    else:
        await chat_unban.finish()

    await chat_unban.finish(f"已将用户 {user_id} 从聊天黑名单中移除")
