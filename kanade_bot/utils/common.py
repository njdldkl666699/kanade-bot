from typing import Literal

from nonebot.adapters.console.event import PrivateMessageEvent as ConsolePrivateMessageEvent


type PlatformType = Literal["console", "onebot"]
"""消息平台类型"""


def console_private_permission(event: ConsolePrivateMessageEvent) -> bool:
    """Console私聊权限检查"""
    return True
