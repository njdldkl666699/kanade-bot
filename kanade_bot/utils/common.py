from typing import Literal

from copilot import CopilotClient
from copilot.client import StopError
from nonebot import get_driver, logger
from nonebot.adapters.console.event import PrivateMessageEvent as ConsolePrivateMessageEvent

type PlatformType = Literal["console", "onebot"]
"""消息平台类型"""


def console_private_permission(event: ConsolePrivateMessageEvent) -> bool:
    """Console私聊权限检查"""
    return True


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
