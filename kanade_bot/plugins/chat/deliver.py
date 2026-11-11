"""消息投递：助手回复解析与分批发送

被动回复与主动发送共用同一套内容解析与批量发送逻辑
"""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlparse

from nonebot import logger, require
from nonebot.adapters import Bot
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment

from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme, get_bot_info
from kanade_bot.utils.parse import TextFormat, guess_format
from kanade_bot.utils.session import SessionInfo

from .memes import get_memes

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import render_markdown

type SendFunc = Callable[[OneBotMessage | MessageSegment | str], Awaitable[None]]
"""单条消息发送函数：接收完整消息，负责投递到目标"""


def workspace_root_of(session_id: str) -> Path | None:
    """解析会话的沙箱工作区根（沙箱未启用时为None）

    延迟导入manager：deliver 被 agent.schedule 引用，模块级导入会成环
    """
    from .agent.manager import chat_manager

    if (manager := chat_manager).sandbox_manager is None:
        return None
    return Path(manager.sandbox_manager.workspace_root(session_id))


def _markdown_image_segment(src: str, workspace_root: Path | None) -> MessageSegment | None:
    """把Markdown图片地址解析为图片消息段，无法解析时返回None

    本地路径仅支持工作区内路径（绝对路径与相对路径都限制在工作区根内），
    工作区以外的本地路径一律拒绝。
    """
    scheme = urlparse(src).scheme
    if scheme in ("http", "https"):
        return MessageSegment.image(src)

    if scheme:
        logger.warning("回复中的Markdown图片地址协议不支持: {}", src)
        return None

    if workspace_root is None:
        logger.warning("回复中的Markdown图片为本地路径但未提供工作区根: {}", src)
        return None
    root = workspace_root.resolve()

    if src.startswith("/"):
        # 绝对路径仅支持工作区内：先按宿主路径解析，未命中时按沙箱式
        # （沙箱内以工作区根为 /）以工作区相对路径再试一次，最终都必须落在工作区内
        path = Path(src).resolve()
        if not path.is_file():
            path = (root / src.lstrip("/")).resolve()
    else:
        path = (root / src).resolve()

    if not path.is_relative_to(root):
        logger.warning("回复中的Markdown图片路径越出工作区: {}", src)
        return None

    if not path.is_file():
        logger.warning("回复中的Markdown图片文件不存在: {}", path)
        return None
    return MessageSegment.image(path.read_bytes())


async def _meme_segment(desc: str) -> MessageSegment | None:
    """按描述语义检索一张表情包，未启用/未检索到时返回None"""
    if (memes := get_memes()) is None:
        logger.warning("表情包索引未启用，丢弃占位符：{{表情: {}}}", desc)
        return None
    try:
        rec = await memes.select(desc)
    except Exception:
        logger.exception("表情包检索失败：{{表情: {}}}", desc)
        return None
    if rec is None:
        logger.warning("未检索到匹配的表情包，丢弃占位符：{{表情: {}}}", desc)
        return None
    return OneBotMessageSegmentMeme(memes.file_path(rec))


MEME_PLACEHOLDER = re.compile(r"\{\{表情[:：]\s*([^{}]+?)\s*\}\}")
"""表情包引用占位符：`{{表情: 自然语言描述}}`，投递时按语义检索选图"""


AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]|[@](\d{5,11})")
"""@消息段匹配：兼容 `[CQ:at,qq=10001000]` 与 `@123456` 两种写法

裸@格式限定5-11位数字，避免误吞 `@2x` 这类短数字
"""


async def _text_segments(text: str, code_blocks: list[str]) -> list[MessageSegment]:
    """把一段文本解析为消息段：还原代码块，表情包引用与@按原始位置替换为对应消息段"""
    # 替换回代码块（使用正则确保只替换占位符）
    for i, code_block in enumerate(code_blocks):
        text = text.replace(f"__CODE_BLOCK_{i}__", code_block)

    # 表情包引用与@各取首个，按出现位置先后处理
    candidates: list[tuple[int, str, re.Match]] = []
    if (m := MEME_PLACEHOLDER.search(text)) is not None:
        candidates.append((m.start(), "meme", m))
    if (m := AT_RE.search(text)) is not None:
        candidates.append((m.start(), "at", m))
    candidates.sort(key=lambda item: item[0])

    segments: list[MessageSegment] = []
    pos = 0
    for _, kind, m in candidates:
        if prefix := text[pos : m.start()].strip():
            segments.append(MessageSegment.text(prefix))
        if kind == "meme":
            if seg := await _meme_segment(m.group(1)):
                segments.append(seg)
        else:
            segments.append(MessageSegment.at(int(m.group(1) or m.group(2))))
        pos = m.end()
    if rest := text[pos:].strip():
        segments.append(MessageSegment.text(rest))
    return segments


MARKDOWN_IMAGE_RE = re.compile(r'!\[([^\]\n]*)\]\(\s*([^)\s]+)(?:\s+"[^"]*")?\s*\)')
"""Markdown图片语法 `![描述](地址)` 的匹配模式：地址支持网络URL与工作区内路径"""


async def parse_reply_messages(
    content: str, workspace_root: Path | None = None
) -> list[OneBotMessage]:
    """把一段助手回复文本解析为消息列表（每个元素作为一条消息发送）

    1. 按两个及以上换行拆分，代码块先替换为占位符保护
    2. `![描述](地址)` Markdown图片语法替换为图片消息段
       独立成行的块级图片单独作为一条消息；
       行内图片与所在文本按原始顺序合并进同一条消息；
       失效图降级为文字提示
    3. 还原代码块占位符为原始代码块
    4. `{{表情: 描述}}`引用替换为动画表情消息段（向量检索）
    5. `[CQ:at,qq=10001000]`或`@123456`替换为at消息段
    """
    # 用于存储最终的消息
    messages: list[OneBotMessage] = []

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
        # 先按顺序提取Markdown图片（此时代码块仍为占位符，图片语法不会被误处理），
        # 生成文本/图片交替的元素序列，并标记图片是否独立成行
        elements: list[tuple[str | MessageSegment, bool]] = []
        pos = 0
        for m in MARKDOWN_IMAGE_RE.finditer(chunk):
            if text := chunk[pos : m.start()]:
                elements.append((text, False))
            # 同一行内除空白外无其他内容 → 独立成行的块级图片
            line_start = chunk.rfind("\n", 0, m.start()) + 1
            line_end = chunk.find("\n", m.end())
            if line_end == -1:
                line_end = len(chunk)
            is_block = not (
                chunk[line_start : m.start()].strip() or chunk[m.end() : line_end].strip()
            )
            if segment := _markdown_image_segment(m.group(2), workspace_root):
                elements.append((segment, is_block))
            else:
                # 失效图降级为文字提示，随所在文本合并在同一消息里
                elements.append((f"[图片失效：{m.group(2)}]", False))
            pos = m.end()
        if text := chunk[pos:]:
            elements.append((text, False))

        # 块级图片单独成消息；行内图片与文本按原始顺序合并为一条消息
        message = OneBotMessage()
        for element, is_block in elements:
            if isinstance(element, MessageSegment):
                if is_block:
                    if message:
                        messages.append(message)
                        message = OneBotMessage()
                    messages.append(OneBotMessage(element))
                else:
                    message += element
            else:
                message += await _text_segments(element, code_blocks)
        if message:
            messages.append(message)

    return messages


async def send_messages(
    send: SendFunc,
    bot: OneBot,
    messages: list[OneBotMessage],
    *,
    reply: MessageSegment | None = None,
    content_long: bool = False,
    content_format: TextFormat = "plaintext",
):
    """按消息数量分级批量发送

    send: 单条消息发送函数；
    reply: 引用回复，非 None 时仅拼在第一条消息前
    """
    # 根据消息的数量决定发送方式
    if not messages:
        return

    # 消息数<=5，按条发送
    if len(messages) <= 5:
        for i, message in enumerate(messages):
            if reply is not None and i == 0:
                await send(reply + message)
            else:
                await send(message)

    # 消息数>5但<=10，合并转发
    elif len(messages) <= 10:
        info = await get_bot_info(bot)
        node_custom_message = OneBotMessage()
        for message in messages:
            node_custom_message += MessageSegment.node_custom(*info, message)
        await send(node_custom_message)

    # 消息数>10，合并相邻的纯文本消息
    else:
        merged: list[OneBotMessage | str] = []
        sentinel: str = ""
        for message in messages:
            if message and all(seg.type == "text" for seg in message):
                sentinel += "".join(seg.data["text"] for seg in message) + "\n\n"
            else:
                if sentinel := sentinel.strip():
                    merged.append(sentinel)
                    sentinel = ""
                merged.append(message)
        if sentinel := sentinel.strip():
            merged.append(sentinel)

        # 内容不长，直接发送消息列表
        if not content_long:
            for i, message in enumerate(merged):
                if reply is not None and i == 0:
                    await send(reply + message)
                else:
                    await send(message)
            return

        # 内容长的Markdown消息，转换为图片发送
        if len(merged) == 1 and isinstance(m := merged[0], str) and content_format == "markdown":
            message = MessageSegment.image(bytes(await render_markdown(m)))
            if reply is not None:
                message += reply
            await send(message)
            return

        # 内容长的纯文本，作为合并转发消息发送
        node_custom_message = OneBotMessage()
        info = await get_bot_info(bot)
        for message in merged:
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

    await send_messages(
        _send,
        bot,
        await parse_reply_messages(content, workspace_root_of(session_info.session_id)),
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
