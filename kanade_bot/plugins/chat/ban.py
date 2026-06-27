from typing import Literal

from nonebot.adapters import Message, MessageSegment

from kanade_bot.utils.common import PlatformType

from .config import chat_configs

type BanType = Literal["user", "group"]


def parse_ban_args(arg_msg: Message) -> tuple[str, BanType] | None:
    """解析聊天黑名单命令的参数，返回ID和类型"""
    ban_type: BanType = "user"
    if len(arg_msg) == 1:
        arg: MessageSegment = arg_msg[0]

        if arg.type == "text":
            args: list[str] = arg.data["text"].strip().split(maxsplit=1)
            if len(args) == 0:
                return None

            id = args[0]

            type_str = args[1] if len(args) > 1 else "user"
            if type_str.lower() == "group":
                return id, "group"
            return id, "user"

        if arg.type == "at":
            id = arg.data["qq"].strip()
            return id, "user"

    if len(arg_msg) >= 2:
        # 取前两个参数，解析ID和类型
        id_arg: MessageSegment = arg_msg[0]
        id: str = ""
        if id_arg.type == "text":
            id = id_arg.data["text"].strip()
        elif id_arg.type == "at":
            id = id_arg.data["qq"].strip()

        type_arg: MessageSegment = arg_msg[1]
        ban_type: BanType = "user"
        if type_arg.type == "text":
            type_str = type_arg.data["text"].strip().lower()
            if type_str == "group":
                ban_type = "group"

        if id:
            return id, ban_type

    return None


def _get_ban_list(ban_type: BanType, platform: PlatformType):
    ban_list: set[str] = set()

    config = chat_configs.instance.get_by_platform(platform)

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
    chat_configs.save_to_file()


def remove_from_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = _get_ban_list(ban_type, platform)
    ban_list.discard(id)
    chat_configs.save_to_file()
