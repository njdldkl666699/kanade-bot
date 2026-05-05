from io import BytesIO
from pathlib import Path
from typing import Any, Literal, SupportsIndex

from nonebot.adapters import Bot, Event
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.console.event import PrivateMessageEvent as ConsolePrivateMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from pydantic import BaseModel


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


def OneBotMessageSegmentMeme(file: str | bytes | BytesIO | Path) -> OneBotMessageSegment:
    """创建一个OneBot动画表情消息段"""
    message = OneBotMessageSegment.image(file)
    message.data["summary"] = "[动画表情]"
    message.data["sub_type"] = 1
    return message


type PlatformType = Literal["console", "onebot"]


class SessionInfo(BaseModel):
    session_id: str
    """会话ID，格式为'{平台}-{会话类型}-{唯一标识}'"""
    platform: PlatformType | None = None
    """消息平台类型"""
    nickname: str | None = None
    """用户昵称"""
    user_id: str | None = None
    """用户ID"""
    group_name: str | None = None
    """群聊名称"""
    group_id: str | None = None
    """群聊ID"""


async def extract_session_info(event: Event, bot: Bot | None = None) -> SessionInfo:
    """解析事件以提取会话信息"""
    info = SessionInfo(session_id=event.get_session_id())

    # 处理OneBot消息事件
    if isinstance(event, OneBotMessageEvent):
        uid = event.user_id
        info.session_id = f"qq-private-{uid}"
        info.nickname = event.sender.nickname
        info.user_id = str(uid)
        info.platform = "onebot"
    if isinstance(event, OneBotGroupMessageEvent):
        gid = event.group_id
        info.session_id = f"qq-group-{gid}"
        info.nickname = event.sender.card or event.sender.nickname
        info.group_id = str(gid)
        if isinstance(bot, OneBot):
            group_info = await bot.get_group_info(group_id=gid)
            info.group_name = group_info.get("group_name") if group_info else None

    # Console的消息事件
    if isinstance(event, ConsoleMessageEvent):
        uid = event.user.id
        info.session_id = f"console-private-{uid}"
        info.nickname = event.user.nickname
        info.user_id = uid
        info.platform = "console"
    if isinstance(event, ConsolePublicMessageEvent):
        gid = event.channel.id
        info.session_id = f"console-group-{gid}"
        info.group_name = event.channel.name
        info.group_id = gid

    return info


def build_sender_info(name: str | None, id: str | None) -> str:
    """构建发送者信息字符串"""
    parts: list[str] = []
    if name:
        parts.append(name)
    if id:
        parts.append(f"[id={id}]")
    return "".join(parts)


async def set_msg_emoji_like(
    bot: OneBot,
    message_id: int,
    emoji_id: int,
    set: bool = True,
):
    """设置表情回复

    :param message_id: 消息ID
    :param emoji_id: 表情ID
    """
    await bot.call_api(
        "set_msg_emoji_like",
        message_id=message_id,
        emoji_id=emoji_id,
        set=set,
    )


async def send_poke(
    bot: OneBot,
    user_id: int | str,
    group_id: int | None = None,
):
    """发送戳一戳

    :param user_id: 目标用户ID
    :param group_id: 群聊ID（如果是群聊内戳人则需要提供）
    """
    await bot.call_api(
        "send_poke",
        user_id=user_id,
        group_id=group_id,
    )


def console_private_permission(event: ConsolePrivateMessageEvent) -> bool:
    """Console私聊权限检查"""
    return True
