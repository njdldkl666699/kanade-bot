from nonebot.adapters import Bot, Event
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from pydantic import BaseModel

from .common import PlatformType


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
