from typing import Literal

from nonebot.adapters import Event
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent

from ..util import PlatformType
from .config import configs, write_chat_config

type BanType = Literal["user", "group"]


def get_ban_list(ban_type: BanType, platform: PlatformType):
    ban_list: set[str] = set()

    if platform == "console":
        config = configs.console
    elif platform == "onebot":
        config = configs.onebot

    if ban_type == "user":
        ban_list = config.banned_users
    elif ban_type == "group":
        ban_list = config.banned_groups

    return ban_list


def is_banned(id: str, ban_type: BanType, platform: PlatformType) -> bool:
    return id in get_ban_list(ban_type, platform)


def is_event_banned(event: Event):
    """检查用户或群聊是否在聊天黑名单中"""
    # 确定平台类型
    platform: PlatformType | None = None
    if isinstance(event, ConsoleMessageEvent):
        platform = "console"
    elif isinstance(event, OneBotMessageEvent):
        platform = "onebot"
    else:
        return False

    # 检查群聊是否在聊天黑名单中
    ban_type = "group"
    group_id: str | None = None
    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = str(event.group_id)

    if group_id and is_banned(group_id, ban_type, platform):
        return True

    # 检查用户是否在聊天黑名单中
    ban_type = "user"
    user_id: str = event.get_user_id()
    if user_id and is_banned(user_id, ban_type, platform):
        return True


def add_to_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = get_ban_list(ban_type, platform)
    ban_list.add(id)
    write_chat_config()


def remove_from_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = get_ban_list(ban_type, platform)
    ban_list.discard(id)
    write_chat_config()
