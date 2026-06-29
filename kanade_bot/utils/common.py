from typing import Literal

from copilot import CopilotClient
from copilot.client import StopError
from nonebot import get_driver, logger
from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.params import EventToMe

type PlatformType = Literal["console", "onebot"]
"""消息平台类型"""


def get_platform_type(event: Event) -> PlatformType:
    """根据事件类型确定消息平台"""
    if isinstance(event, ConsoleEvent):
        return "console"
    elif isinstance(event, OneBotEvent):
        return "onebot"
    else:
        raise ValueError(f"Unsupported event type: {type(event)}")


def group_permission(event: OneBotGroupMessageEvent | ConsolePublicMessageEvent) -> bool:
    """匹配群聊消息类型事件"""
    return True


def not_to_me(to_me: bool = EventToMe()):
    """匹配与机器人无关的消息"""
    return not to_me


COPILOT_CLIENT = CopilotClient()
"""全局Copilot客户端单例

负责与Copilot服务进行通信，创建和恢复会话等操作
"""


driver = get_driver()


@driver.on_startup
async def startup():
    await COPILOT_CLIENT.start()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    try:
        await COPILOT_CLIENT.stop()
    except* StopError as eg:
        logger.warning(f"停止Copilot客户端时发生错误: {eg.message}")
    logger.info("Copilot客户端已关闭")
