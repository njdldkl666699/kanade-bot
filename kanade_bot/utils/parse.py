from base64 import b64encode
from typing import Any, SupportsIndex

from copilot.session import Attachment
from httpx import AsyncClient
from nonebot.adapters import Message
from nonebot.adapters.console import Message as ConsoleMessage
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage


def parse_arg_message(
    arg_str: str,
    mappings: dict[str, type] | None = None,
    maxsplit: SupportsIndex = -1,
) -> dict[str, Any]:
    """解析命令参数消息

    参数:
        arg_str: 命令参数消息
        mappings: 可选的参数名称映射字典，键为参数名称，值为参数类型
        maxsplit: 分割参数消息时的最大分割次数，默认为 -1（不限制）

    返回:
        dict: 解析后的参数字典，键为参数名称，值为参数值；\
            如果参数值无法转换为指定类型或缺失，则值为 None

    示例:

        >>> parse_arg_message(Message("北京 3"), {"query": str, "days": int})
        {"query": "北京", "days": 3}
        >>> parse_arg_message(Message("上海"), {"query": str, "days": int})
        {"query": "上海", "days": None}

    """
    if not mappings:
        return {}

    args = arg_str.strip().split(maxsplit=maxsplit)
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


async def parse_onebot_message_for_ai(
    message: OneBotMessage,
    client: AsyncClient | None = None,
    bot: OneBot | None = None,
) -> tuple[str, list[Attachment]]:
    """解析OneBot消息，返回AI可读的文本和附件列表

    :param client: 用于请求图片的HTTP客户端，如果需要提取图片附件则必须提供
    :param bot: 可选的OneBot实例，如果提供则可以解析转发消息中的发送者信息
    """
    text_parts: list[str] = []
    attachments: list[Attachment] = []

    # 如果消息只有一个segment且是转发消息，直接解析转发消息中的内容
    if len(message) == 1 and message[0].type == "forward" and bot:
        forward_id: str = message[0].data["id"]
        forward_msg = await bot.get_forward_msg(id=forward_id)

        forward_text, forward_attachments = await parse_onebot_message_for_ai(
            forward_msg["message"], client, bot
        )
        text_parts.append(f"<forward id={forward_id}>")
        text_parts.append(forward_text)
        text_parts.append("</forward>")
        attachments.extend(forward_attachments)

        return "\n".join(text_parts), attachments

    # 处理普通消息，解析其中的图片附件
    last_image_index = -1
    for i, segment in enumerate(message):
        if segment.type != "image":
            continue

        image: Attachment = {
            "type": "blob",
            "data": "",
            "mimeType": "application/octet-stream",
            "displayName": segment.data["file"] or "image.png",
        }
        if client:
            url: str = segment.data["url"]
            response = await client.get(url)
            response.raise_for_status()
            image["data"] = b64encode(response.content).decode()
            image["mimeType"] = response.headers.get("Content-Type", "application/octet-stream")

        # 把前半段内容转为文字
        splitted_segments = message[:i]
        if splitted_segments:
            text_parts.append(splitted_segments.to_rich_text().strip())

        # 添加图片附件
        last_image_index = i
        attachments.append(image)
        text_parts.append(f"[图片：{image['displayName']}]")

    # 添加最后剩余的文本内容，没有图片则直接转换整个消息为文本
    # == -1 : 整个消息段列表都不是图片
    # == len(message) - 1 : 最后一个是图片消息段
    # 但这些边缘情况都可以通过切片自动处理，所以直接切最后一个图片消息段的下一个位置到末尾即可
    remaining_segments = message[last_image_index + 1 :]
    if remaining_segments:
        text_parts.append(remaining_segments.to_rich_text().strip())

    return "\n".join(text_parts), attachments


async def parse_message_for_ai(
    message: Message,
    client: AsyncClient | None = None,
    bot: OneBot | None = None,
) -> tuple[str, list[Attachment]]:
    """解析消息，返回AI可读的文本和附件列表

    :param client: 用于请求图片的HTTP客户端，如果需要提取图片附件则必须提供
    :param bot: 可选的OneBot实例，如果提供则可以解析转发消息中的发送者信息
    """
    prompt: str = ""
    attachments: list[Attachment] = []
    ## Console消息直接发送原始内容，无需解析附件
    if isinstance(message, ConsoleMessage):
        prompt = str(message)

    ## OneBot消息
    # 处理发送消息
    if isinstance(message, OneBotMessage):
        prompt, attachments = await parse_onebot_message_for_ai(message, client, bot)

    return prompt, attachments
