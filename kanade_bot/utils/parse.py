import base64
from dataclasses import dataclass
from typing import Any, Literal, SupportsIndex

import magic
from markdown_it import MarkdownIt
from nonebot import logger
from nonebot.adapters import Event
from nonebot.adapters.console import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.onebot.v11 import ActionFailed, GroupMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.adapters.onebot.v11.event import Reply

from kanade_bot.utils.common import QQ_EMOJI_INDEXES

from .onebot11 import get_image_path
from .session import extract_session_info


@dataclass
class ImageInput:
    """图片附件"""

    name: str
    data: str | None = None
    """图片内容的Base64字符串，无法获取图片内容时为None"""
    mime_type: str | None = None


def parse_arg_message(
    arg_str: str,
    mappings: dict[str, type] | None = None,
    *,
    sep: str | None = None,
    maxsplit: SupportsIndex = -1,
) -> dict[str, Any]:
    """解析命令参数消息

    Args:
        arg_str: 命令参数消息
        mappings: 可选的参数名称映射字典，键为参数名称，值为参数类型
        sep: 分割参数消息的分隔符，默认为 None（按空白字符分割）
        maxsplit: 分割参数消息时的最大分割次数，默认为 -1（不限制）

    Returns:
        dict: 解析后的参数字典，键为参数名称，值为参数值；\n
            如果参数值无法转换为指定类型或缺失，则值为 None

    Examples:
        >>> parse_arg_message(Message("北京 3"), {"query": str, "days": int})
        {"query": "北京", "days": 3}
        >>> parse_arg_message(Message("上海"), {"query": str, "days": int})
        {"query": "上海", "days": None}

    """
    if not mappings:
        return {}

    args = arg_str.strip().split(sep=sep, maxsplit=maxsplit)
    arg_dict: dict[str, Any] = {}

    for index, (name, value_type) in enumerate(mappings.items()):
        if index >= len(args):
            arg_dict[name] = None
            continue

        raw_value = args[index]
        try:
            arg_dict[name] = value_type(raw_value)
        except (TypeError, ValueError):
            arg_dict[name] = None

    return arg_dict


def bool_from_str(s: str | None) -> bool:
    """将字符串转换为布尔值，支持常见的真值和假值表示"""
    if s is None:
        return False
    s = s.strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"无法将字符串 '{s}' 转换为布尔值")


def build_sender_info(name: str | None, id: str | None) -> str:
    """构建发送者信息字符串"""
    parts: list[str] = []
    if name:
        parts.append(name)
    if id:
        parts.append(f"[id={id}]")
    return "".join(parts)


md = MarkdownIt()

ALLOWED_BLOCK_TOKENS = {
    "paragraph_open",
    "paragraph_close",
    "inline",
}
"""纯文本段落中允许出现的 block token"""

ALLOWED_INLINE_TOKENS = {
    "text",
    "softbreak",
    "hardbreak",
}
"""纯文本中允许出现的 inline token"""


type TextFormat = Literal["markdown", "plaintext"]


def guess_format(text: str) -> TextFormat:
    """
    启发式判断：返回文本类型。

    判断依据：是否出现了 Markdown 特有的语法结构。
    注意：如果纯文本中恰好写了 **粗体**，它也会被判定为 markdown，
    因为从语法上讲它就是 Markdown。
    """
    tokens = md.parse(text)

    for token in tokens:
        # 检查块级 token
        if token.type not in ALLOWED_BLOCK_TOKENS:
            return "markdown"

        # 检查行内 token
        if token.type == "inline" and token.children:
            for child in token.children:
                if child.type not in ALLOWED_INLINE_TOKENS:
                    return "markdown"

    return "plaintext"


async def get_forward_message_events(
    bot: OneBot, fwd_segment: OneBotMessageSegment
) -> tuple[str, list[OneBotMessageEvent]]:
    """获取合并转发消息中的消息事件列表

    Returns:
        str: 转发消息的id
        list[OneBotMessageEvent]: 转发消息事件列表
    """
    assert fwd_segment.type == "forward", "fwd_segment 必须是转发消息段"

    fwd_id: str = fwd_segment.data["id"]
    raw_fwd_msg_events: list[dict[str, Any]] = []
    if "content" in fwd_segment.data:
        # 情况一：有content字段，直接视作fwd_msg_events使用
        # 这种情况我测试下是message是从reply中拿到的，content里直接是转发消息事件列表
        # 这种情况可以拿到嵌套合并转发的消息事件
        raw_fwd_msg_events = fwd_segment.data["content"]
    else:
        # 如果message直接是event.message，就没有content字段
        # 这种情况下无法获取嵌套合并转发。
        try:
            forward_response = await bot.get_forward_msg(id=fwd_id)
            raw_fwd_msg_events = forward_response["messages"]
        except ActionFailed as e:
            logger.warning("获取转发消息失败: {}", e)

    fwd_msg_events: list[OneBotMessageEvent] = []
    for e in raw_fwd_msg_events:
        e["post_type"] = "message"
        fwd_msg_events.append(OneBotMessageEvent.model_validate(e))
    return fwd_id, fwd_msg_events


async def format_forward_message_for_ai(
    bot: OneBot,
    fwd_segment: OneBotMessageSegment,
    *,
    fetch_images: bool = True,
) -> tuple[str, list[ImageInput]]:
    """把一条合并转发消息展开为AI可读文本

    逐条复用 `parse_onebot_message_for_ai` 解析，附带发送者信息，嵌套转发递归展开。

    :param fetch_images: 是否拉取子消息中的图片内容作为附件；False 时仅保留
        `[图片：文件名]` 占位（供 agent 后续按文件名单独查看）
    """
    fwd_id, fwd_msg_events = await get_forward_message_events(bot, fwd_segment)

    text_parts: list[str] = [f"<forward id={fwd_id}>"]
    attachments: list[ImageInput] = []

    for e in fwd_msg_events:
        text, fwd_attachments = await parse_onebot_message_for_ai(e, bot, fetch_images=fetch_images)

        # 附带发送者信息
        session_info = await extract_session_info(e, bot)
        if user_info := build_sender_info(session_info.nickname, session_info.user_id):
            text = f"{user_info}：{text}"

        text_parts.append(text)
        attachments.extend(fwd_attachments)

    text_parts.append("</forward>")

    return "\n".join(text_parts), attachments


async def parse_onebot_message_for_ai(
    event_or_reply: OneBotMessageEvent | Reply,
    bot: OneBot | None = None,
    *,
    fetch_images: bool = True,
) -> tuple[str, list[ImageInput]]:
    """解析OneBot消息，返回AI可读的文本和附件列表

    :param bot: 可选的OneBot实例，如果提供则可以解析转发消息中的发送者信息，并获取图片附件
    :param fetch_images: 是否拉取图片内容作为附件；False 时仅保留 `[图片：文件名]` 占位
    """
    message = event_or_reply.message
    text_parts: list[str] = []
    attachments: list[ImageInput] = []

    # 如果消息只有一个segment且是转发消息，直接解析转发消息中的内容
    if len(message) == 1 and message[0].type == "forward" and bot:
        return await format_forward_message_for_ai(bot, message[0], fetch_images=fetch_images)

    # 逐段处理消息：图片解析为附件并留下占位文本，其余消息段按类型格式化为文字
    for segment in message:
        if segment.type == "image":
            file: str = segment.data["file"]
            attachment: ImageInput | None = None
            if bot and fetch_images:
                try:
                    local_path = await get_image_path(bot, segment)
                except Exception as e:
                    logger.warning("获取图片 {} 失败: {}", file, e)
                    local_path = None
                # 文件不存在或内容为空时都不构造附件，仅保留文本占位
                if local_path and local_path.is_file() and (raw := local_path.read_bytes()):
                    attachment = ImageInput(
                        name=file or "image.png",
                        data=base64.b64encode(raw).decode(),
                        mime_type=magic.from_buffer(raw, mime=True),
                    )
            if attachment:
                attachments.append(attachment)
            text_parts.append(f"[图片：{file}]")

        # 在这里可以对非文本消息段进行各种格式化处理以保留更多信息
        elif segment.type == "at":
            qq: str = segment.data["qq"]
            if qq == "all":
                text_parts.append("@全体成员 ")
                continue
            if not bot:
                text_parts.append(f"@{qq} ")
                continue
            if not isinstance(event_or_reply, GroupMessageEvent):
                text_parts.append(f"@{qq} ")
                continue

            name = qq
            try:
                member_info = await bot.get_group_member_info(
                    group_id=event_or_reply.group_id, user_id=int(qq)
                )
                nickname = member_info["nickname"]
                card = member_info["card"]
                name = card or nickname or qq
            finally:
                text_parts.append(f"@{name} ")
        elif segment.type == "face":
            id = str(segment.data["id"])
            description = QQ_EMOJI_INDEXES.get(id, id)
            text_parts.append(f"[表情 {description}]")
        else:
            # 空文本段（如 reply 段等）不产生空行
            if rich_text := segment.to_rich_text(truncate=None).strip():
                text_parts.append(rich_text)

    return "\n".join(text_parts), attachments


async def parse_message_for_ai(
    event: Event,
    bot: OneBot | None = None,
) -> tuple[str, list[ImageInput]]:
    """解析消息，返回AI可读的文本和附件列表

    :param bot: 可选的OneBot实例，如果提供则可以解析转发消息中的发送者信息，并获取图片附件
    """
    prompt: str = ""
    attachments: list[ImageInput] = []
    ## Console消息直接发送原始内容，无需解析附件
    if isinstance(event, ConsoleMessageEvent):
        prompt = str(event.message)

    ## OneBot消息
    # 处理发送消息
    if isinstance(event, OneBotMessageEvent):
        prompt, attachments = await parse_onebot_message_for_ai(event, bot)

    return prompt, attachments
