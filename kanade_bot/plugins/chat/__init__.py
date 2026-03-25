import re
from base64 import b64encode

from copilot import Attachment
from nonebot import get_driver, get_plugin_config, logger, on_command, on_message
from nonebot.adapters import Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, EventPlainText, EventToMe
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from kanade_bot.plugins.chat.ban import (
    add_user_to_ban_list,
    is_user_banned,
    remove_user_from_ban_list,
)

from .client import client
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
        session_id = f"qq-private-{event.user_id}"
        nickname = event.sender.nickname
    if isinstance(event, OneBotGroupMessageEvent):
        session_id = f"qq-group-{event.group_id}"
        nickname = event.sender.card or event.sender.nickname
        is_group = True

    # Console的消息事件
    if isinstance(event, ConsoleMessageEvent):
        nickname = event.user.nickname
        session_id = f"console-private-{event.user.id}"
    if isinstance(event, ConsolePublicMessageEvent):
        session_id = f"console-group-{event.channel.id}"
        is_group = True

    prompt = f"{nickname}说：{prompt}" if nickname else prompt
    return session_id, prompt, is_group


async def resolve_message_images(message: OneBotMessage) -> list[Attachment]:
    """解析消息中的图片并返回附件列表"""
    attachments: list[Attachment] = []
    for segment in message:
        if segment.type != "image":
            continue

        displayName: str = segment.data["file"] or "image.png"
        url: str | None = segment.data["url"]
        if not url:
            continue

        response = await client.get(url)
        response.raise_for_status()
        data = b64encode(response.content).decode()
        attachments.append(
            {
                "type": "blob",
                "data": data,
                "mimeType": "image/png",
                "displayName": displayName,
            }
        )
    return attachments


def split_content_preserving_code_blocks(content):
    # 用于存储最终的块
    chunks = []

    # 找到所有代码块的位置，将它们替换为占位符
    code_blocks = []

    # 匹配 ```...``` 代码块（支持带语言标识）
    def replace_code_block(match):
        code_blocks.append(match.group(0))
        # 返回一个唯一占位符
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    # 先保护代码块，将代码块替换为占位符
    content_with_placeholders = re.sub(r"```[\s\S]*?```", replace_code_block, content)

    # 按两个及以上换行拆分（代码块已被保护）
    temp_chunks = [
        chunk for chunk in re.split(r"(?:\r?\n){2,}", content_with_placeholders) if chunk.strip()
    ]

    # 恢复每个块中的代码块
    for chunk in temp_chunks:
        restored_chunk = chunk
        # 替换回代码块（使用正则确保只替换占位符）
        for i, code_block in enumerate(code_blocks):
            restored_chunk = restored_chunk.replace(f"__CODE_BLOCK_{i}__", code_block)
        chunks.append(restored_chunk)

    return chunks


async def send_message_in_chunks(
    matcher: type[Matcher],
    event: Event,
    session_id: str,
    prompt: str = EventPlainText(),
    is_group: bool = False,
):
    # 处理消息中的图片附件
    attachments: list[Attachment] = []
    # 1. 回复的消息中的图片
    if isinstance(event, OneBotMessageEvent) and event.reply:
        reply_message_attachments = await resolve_message_images(event.reply.message)
        attachments.extend(reply_message_attachments)
    # 2. 发送的消息中的图片
    message = event.get_message()
    if isinstance(message, OneBotMessage):
        message_attachments = await resolve_message_images(message)
        attachments.extend(message_attachments)

    # 处理引用（回复）消息中的文本内容
    reply_text: str | None = None
    if isinstance(event, OneBotMessageEvent) and event.reply:
        reply_text = event.reply.message.extract_plain_text().strip()

    response, new_session = await copilot.send_and_wait(
        session_id,
        prompt,
        is_group=is_group,
        reply_text=reply_text,
        attachments=attachments,
        timeout=300,
    )
    if new_session:
        logger.info(f"会话{session_id}是新会话，旧会话可能被手动删除或损坏")

    if response and response.data.content:
        content = response.data.content
        chunks = split_content_preserving_code_blocks(content)
        # 消息数<=3，按条发送
        if len(chunks) <= 3:
            for chunk in chunks:
                await matcher.send(chunk)
            await matcher.finish()
        # 消息数>3
        # Console合并成一条发送
        if isinstance(event, ConsoleMessageEvent):
            await matcher.finish(content)
        # OneBot分成多条，合并转发
        if isinstance(event, OneBotMessageEvent):
            message = OneBotMessage()
            for chunk in chunks:
                message += MessageSegment.node_custom(cfg.chat_bot_id, cfg.chat_bot_nickname, chunk)
            await matcher.finish(message)
        await matcher.finish()
    else:
        await matcher.finish("模型未响应，请稍后再试")


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
    await send_message_in_chunks(chat, event, session_id, prompt, is_group)


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
    session_id, prompt, is_group = resolve_session_id_and_prompt(event, prompt)

    # 如果缓冲区大小超过限制，主动发送消息
    size = copilot.get_session_prompt_buffer_size(session_id)
    cfg_size = cfg.chat_prompt_buffer_size
    # cfg_size <= 0表示不限制缓冲区大小
    if cfg_size > 0 and size >= cfg_size:
        logger.info(f"会话{session_id}的消息缓冲区已达{size}条，主动发送消息")
        await send_message_in_chunks(chat_monitor, event, session_id, prompt, is_group)
        # return
    # 否则将用户消息添加到会话缓冲区
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
    if admin_id not in global_config.superusers:
        admin_operates = False
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
