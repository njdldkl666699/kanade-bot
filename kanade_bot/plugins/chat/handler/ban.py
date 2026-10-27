"""聊天黑名单命令：拉黑/解除拉黑（SUPERUSER）"""

from nonebot.adapters import Event, Message, MessageSegment
from nonebot.params import CommandArg

from kanade_bot.utils.common import PlatformType, get_platform_type

from ..config import chat_configs
from ..matcher import chat_ban, chat_unban
from ..util import BanType, get_ban_list


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


def add_to_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = get_ban_list(ban_type, platform)
    ban_list.add(id)
    chat_configs.save_to_file()


def remove_from_ban_list(id: str, ban_type: BanType, platform: PlatformType):
    ban_list = get_ban_list(ban_type, platform)
    ban_list.discard(id)
    chat_configs.save_to_file()


@chat_ban.handle()
async def handle_chat_ban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_ban.finish()
    id, ban_type = args

    platform = get_platform_type(event)
    add_to_ban_list(id, ban_type, platform)

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_ban.finish(f"已将{type_text} {id} 添加到聊天黑名单")


@chat_unban.handle()
async def handle_chat_unban(event: Event, arg_msg: Message = CommandArg()):
    args = parse_ban_args(arg_msg)
    if not args:
        await chat_unban.finish()
    id, ban_type = args

    platform = get_platform_type(event)
    remove_from_ban_list(id, ban_type, platform)

    type_text = "用户" if ban_type == "user" else "群聊"
    await chat_unban.finish(f"已将{type_text} {id} 从聊天黑名单中移除")
