"""OneBot 消息投递：助手回复解析与分批发送

被动回复（依赖 matcher/event）与主动发送（定时任务等系统触发，仅有会话目标）
共用同一套内容解析与批量发送逻辑。
"""

import random
import re
from collections.abc import Awaitable, Callable

from nonebot import require
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment

from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme, get_bot_info
from kanade_bot.utils.parse import TextFormat, guess_format
from kanade_bot.utils.session import SessionInfo

from .config import cfg, chat_configs

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import md_to_pic

type SendFunc = Callable[[OneBotMessage | MessageSegment | str], Awaitable[None]]
"""单条消息发送函数：接收完整消息，负责投递到目标

接受裸字符串（长文本合并分支的产物）与单个消息段，由实现自行归一化。"""


def extract_segments_preserving_code(content: str) -> list[MessageSegment]:
    """把一段助手回复文本解析为消息段列表

    按两个及以上换行拆分（代码块先替换为占位符保护，拆分后还原），
    并把 `{{表情包名称}}` 引用替换为对应的表情包图片消息段。
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

        # 处理表情包引用，格式{{表情包名称}}
        if meme_match := re.search(r"\{\{(\w+?)\}\}", chunk):
            chunk = chunk.replace(meme_match.group(0), "")
            meme_name = meme_match.group(1)
            if meme_name in chat_configs.instance.memes:
                meme_path = cfg.memes_dir_path / meme_name
                if meme_path.is_dir():
                    image_files = list(meme_path.glob("*"))
                    if image_files:
                        selected_image = random.choice(image_files)
                        segments.append(OneBotMessageSegmentMeme(selected_image))

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

    send: 单条消息发送函数；reply 非 None 时仅拼在第一条消息前（引用回复）
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
    """把一段助手回复主动发送到会话（无需事件与matcher）

    定时任务触发等系统场景使用；发送失败抛出异常由调用方处理。
    """
    if group_id := session_info.group_id:
        target = {"message_type": "group", "group_id": int(group_id)}
    elif user_id := session_info.user_id:
        target = {"message_type": "private", "user_id": int(user_id)}
    else:
        raise ValueError(
            f"会话 {session_info.session_id} 没有可用的群组ID或用户ID，无法主动发送消息"
        )

    async def _send(message: OneBotMessage | MessageSegment | str) -> None:
        # send_msg 只接受 str | Message：消息段归一化为Message，字符串直接发
        if not isinstance(message, OneBotMessage | str):
            message = OneBotMessage(message)
        await bot.send_msg(message=message, **target)

    await send_segments(
        _send,
        bot,
        extract_segments_preserving_code(content),
        content_long=len(content) > 600 or len(content.splitlines()) > 20,
        content_format=guess_format(content),
    )


async def send_text_onebot_proactive(bot: OneBot, session_info: SessionInfo, text: str):
    """把一段纯文本主动发送到会话（不经过表情包/代码块解析）

    用于系统提示类消息（如余额不足告知）。
    """
    if group_id := session_info.group_id:
        await bot.send_msg(message_type="group", group_id=int(group_id), message=text)
    elif user_id := session_info.user_id:
        await bot.send_msg(message_type="private", user_id=int(user_id), message=text)
    else:
        raise ValueError(
            f"会话 {session_info.session_id} 没有可用的群组ID或用户ID，无法主动发送消息"
        )
