from io import BytesIO
from pathlib import Path
from typing import Any, SupportsIndex

from nonebot.adapters import Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg


def parse_arg_message(
    arg_message: Message = CommandArg(),
    mappings: dict[str, type] | None = None,
    maxsplit: SupportsIndex = -1,
) -> dict[str, Any]:
    """解析命令参数消息

    参数:
        arg_message: 命令参数消息
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

    args = arg_message.extract_plain_text().strip().split(maxsplit=maxsplit)
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


def OneBotMessageSegmentMeme(file: str | bytes | BytesIO | Path) -> OneBotMessageSegment:
    """创建一个OneBot动画表情消息段"""
    message = OneBotMessageSegment.image(file)
    message.data["summary"] = "[动画表情]"
    message.data["sub_type"] = 1
    return message


def extract_session_info(event: Event) -> tuple[str, str | None, bool]:
    """解析事件以获取会话ID、用户昵称、是否是群聊"""
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

    return session_id, nickname, is_group
