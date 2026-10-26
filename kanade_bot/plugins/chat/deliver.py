"""消息投递：助手回复解析与分批发送

被动回复与主动发送共用同一套内容解析与批量发送逻辑
"""

import re
from collections.abc import Awaitable, Callable

from nonebot import logger, require
from nonebot.adapters import Bot
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment

from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme, get_bot_info
from kanade_bot.utils.parse import TextFormat, guess_format
from kanade_bot.utils.session import SessionInfo

from .meme_service import get_memes

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import md_to_pic

type SendFunc = Callable[[OneBotMessage | MessageSegment | str], Awaitable[None]]
"""单条消息发送函数：接收完整消息，负责投递到目标"""

MEME_PLACEHOLDER = re.compile(r"\{\{表情[:：]\s*([^{}]+?)\s*\}\}")
"""表情包引用占位符：`{{表情: 自然语言描述}}`，投递时按语义检索选图"""


async def parse_message_segments(content: str) -> list[MessageSegment]:
    """把一段助手回复文本解析为消息段列表

    1. 按两个及以上换行拆分，代码块先替换为占位符保护
    2. 还原代码块占位符为原始代码块
    3. `{{表情: 描述}}`引用替换为动画表情消息段（向量检索）
    4. `[CQ:at,qq=10001000]`替换为at消息段
    """
    # 用于存储最终的块
    segments: list[MessageSegment] = []

    # 找到所有代码块的位置，将它们替换为占位符
    code_blocks = []

    # 匹配 ```...``` 代码块（支持带语言标识）
    def replace_code_block(match):
        code_blocks.append(match.group(0))
        # 返回一个唯一占位符
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    # 先保护代码块，把代码块替换为占位符
    content_with_placeholders = re.sub(r"```[\s\S]*?```", replace_code_block, content)

    # 按两个及以上换行拆分（代码块已被保护）
    temp_chunks = [
        chunk for chunk in re.split(r"(?:\r?\n){2,}", content_with_placeholders) if chunk.strip()
    ]

    for chunk in temp_chunks:
        # 替换回代码块（使用正则确保只替换占位符）
        for i, code_block in enumerate(code_blocks):
            chunk = chunk.replace(f"__CODE_BLOCK_{i}__", code_block)

        # 处理表情包引用：语义检索匹配一张表情包
        if meme_match := MEME_PLACEHOLDER.search(chunk):
            chunk = chunk.replace(meme_match.group(0), "", 1)
            desc = meme_match.group(1)
            if (memes := get_memes()) is None:
                logger.warning("表情包索引未启用，丢弃占位符：{{表情: {}}}", desc)
            else:
                try:
                    rec = await memes.select(desc)
                except Exception:
                    logger.exception("表情包检索失败：{{表情: {}}}", desc)
                    rec = None
                if rec is not None:
                    segments.append(OneBotMessageSegmentMeme(memes.file_path(rec)))
                else:
                    logger.warning("未检索到匹配的表情包，丢弃占位符：{{表情: {}}}", desc)

        # 处理@消息段
        if at_match := re.search(r"\[CQ:at,qq=(\d+)\]", chunk):
            chunk = chunk.replace(at_match.group(0), "")
            qq_id = int(at_match.group(1))
            segments.append(MessageSegment.at(qq_id))

        # 处理后的文本块，如果不为空，则添加为文本消息段
        if chunk.strip():
            segments.append(MessageSegment.text(chunk.strip()))

    return segments


async def send_segments(
    send: SendFunc,
    bot: OneBot,
    segments: list[MessageSegment],
    *,
    reply: MessageSegment | None = None,
    content_long: bool = False,
    content_format: TextFormat = "plaintext",
):
    """按消息段数量分级批量发送

    send: 单条消息发送函数；
    reply: 引用回复，非 None 时仅拼在第一条消息前
    """
    # 根据消息段的数量决定发送方式
    if not segments:
        return

    # 消息数<=5，按条发送
    if len(segments) <= 5:
        for i, segment in enumerate(segments):
            if reply is not None and i == 0:
                await send(reply + segment)
            else:
                await send(segment)

    # 消息数>5但<=10，合并转发
    elif len(segments) <= 10:
        info = await get_bot_info(bot)
        node_custom_message = OneBotMessage()
        for segment in segments:
            node_custom_message += MessageSegment.node_custom(*info, OneBotMessage(segment))
        await send(node_custom_message)

    # 消息数>10，合并相邻的文本消息段
    else:
        messages: list[OneBotMessage | str] = []
        sentinel: str = ""
        for segment in segments:
            if segment.type == "text":
                sentinel += segment.data["text"] + "\n\n"
            else:
                if sentinel := sentinel.strip():
                    messages.append(sentinel)
                    sentinel = ""
                messages.append(OneBotMessage(segment))
        if sentinel := sentinel.strip():
            messages.append(sentinel)

        # 内容不长，直接发送消息列表
        if not content_long:
            for i, message in enumerate(messages):
                if reply is not None and i == 0:
                    await send(reply + message)
                else:
                    await send(message)
            return

        # 内容长的Markdown消息，转换为图片发送
        if (
            len(messages) == 1
            and isinstance(m := messages[0], str)
            and content_format == "markdown"
        ):
            message = MessageSegment.image(await md_to_pic(m))
            if reply is not None:
                message += reply
            await send(message)
            return

        # 内容长的纯文本，作为合并转发消息发送
        node_custom_message = OneBotMessage()
        info = await get_bot_info(bot)
        for message in messages:
            node_custom_message += MessageSegment.node_custom(*info, message)
        await send(node_custom_message)


async def send_onebot_proactive(bot: OneBot, session_info: SessionInfo, content: str):
    """把一段助手回复主动发送到会话

    定时任务触发等系统场景使用；发送失败抛出异常由调用方处理。
    """
    group_id = session_info.group_id
    user_id = session_info.user_id

    if group_id:
        target = {"message_type": "group", "group_id": int(group_id)}
    elif user_id:
        target = {"message_type": "private", "user_id": int(user_id)}
    else:
        raise ValueError(
            f"会话 {session_info.session_id} 没有可用的群组ID或用户ID，无法主动发送消息"
        )

    async def _send(message: OneBotMessage | MessageSegment | str) -> None:
        if not isinstance(message, OneBotMessage | str):
            message = OneBotMessage(message)
        await bot.send_msg(message=message, **target)

    await send_segments(
        _send,
        bot,
        await parse_message_segments(content),
        content_long=len(content) > 600 or len(content.splitlines()) > 20,
        content_format=guess_format(content),
    )


async def send_text_onebot_proactive(bot: OneBot, session_info: SessionInfo, text: str):
    """把一段纯文本主动发送到会话，用于系统提示类消息。"""
    if group_id := session_info.group_id:
        await bot.send_msg(message_type="group", group_id=int(group_id), message=text)
    elif user_id := session_info.user_id:
        await bot.send_msg(message_type="private", user_id=int(user_id), message=text)
    else:
        raise ValueError(
            f"会话 {session_info.session_id} 没有可用的群组ID或用户ID，无法主动发送消息"
        )


async def send_console_proactive(bot: ConsoleBot, session_info: SessionInfo, content: str):
    """把一段文本主动发送到Console会话

    与被动回复保持一致：整段作为纯文本发送，不做分段、表情包展开与合并转发；
    发送失败抛出异常由调用方处理。
    """
    if group_id := session_info.group_id:
        await bot.send_message(channel_id=group_id, message=content)
    elif user_id := session_info.user_id:
        await bot.send_private_message(user_id=user_id, message=content)
    else:
        raise ValueError(
            f"会话 {session_info.session_id} 没有可用的频道ID或用户ID，无法主动发送消息"
        )


async def send_proactive(bot: Bot, session_info: SessionInfo, content: str):
    """把一段助手回复主动发送到会话，按Bot类型分发到对应适配器"""
    if isinstance(bot, ConsoleBot):
        await send_console_proactive(bot, session_info, content)
    elif isinstance(bot, OneBot):
        await send_onebot_proactive(bot, session_info, content)
    else:
        raise TypeError(f"主动发送不支持该适配器的 Bot 类型：{type(bot).__name__}")


async def send_text_proactive(bot: Bot, session_info: SessionInfo, text: str):
    """把一段纯文本主动发送到会话，按Bot类型分发到对应适配器"""
    if isinstance(bot, ConsoleBot):
        await send_console_proactive(bot, session_info, text)
    elif isinstance(bot, OneBot):
        await send_text_onebot_proactive(bot, session_info, text)
    else:
        raise TypeError(f"主动发送不支持该适配器的 Bot 类型：{type(bot).__name__}")
