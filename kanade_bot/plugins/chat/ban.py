from typing import Literal

from nonebot.adapters import Message, MessageSegment

from kanade_bot.utils.common import PlatformType

from .config import chat_configs, write_chat_config

type BanType = Literal["user", "group"]


def _parse_ban_arg_id(arg: MessageSegment) -> str | None:
    """从消息段中解析出ID，支持文本和@消息"""
    if arg.type == "text":
        return arg.data.get("text", "").strip()
    if arg.type == "at":
        return arg.data.get("qq", "").strip()
    return None


def parse_ban_args(arg_msg: Message) -> tuple[str, BanType] | None:
    """解析聊天黑名单命令的参数，返回ID和类型"""
    if len(arg_msg) == 1:
        arg: MessageSegment = arg_msg[0]
        id = _parse_ban_arg_id(arg)
        if id:
            return id, "user"
    if len(arg_msg) >= 2:
        # 取前两个参数，解析ID和类型
        id_arg: MessageSegment = arg_msg[0]
        id = _parse_ban_arg_id(id_arg)

        type_arg: MessageSegment = arg_msg[1]
        ban_type: BanType = "user"
        if type_arg.type == "text":
            type_str = type_arg.data.get("text", "").strip().lower()
            if type_str == "group":
                ban_type = "group"

        if id:
            return id, ban_type

    return None


def _get_ban_list(ban_type: BanType, platform: PlatformType):
    ban_list: set[str] = set()

    config = chat_configs.get_by_platform(platform)

    if ban_type == "user":
        ban_list = config.banned_users
    elif ban_type == "group":
        ban_list = config.banned_groups

    return ban_list


def is_banned(id: str, ban_type: BanType, platform: PlatformType) -> bool:
    return id in _get_ban_list(ban_type, platform)


def add_to_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = _get_ban_list(ban_type, platform)
    ban_list.add(id)
    write_chat_config()


def remove_from_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = _get_ban_list(ban_type, platform)
    ban_list.discard(id)
    write_chat_config()
