import tomllib
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from copilot import CopilotClient, RuntimeConnection
from copilot.client import StopError
from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent

from .schema import KanadeConfig

type PlatformType = Literal["console", "onebot"]
"""消息平台类型"""


def get_platform_type(event: Event) -> PlatformType:
    """根据事件类型确定消息平台"""
    if isinstance(event, ConsoleEvent):
        return "console"
    elif isinstance(event, OneBotEvent):
        return "onebot"
    else:
        raise TypeError(f"Unsupported event type: {type(event)}")


def asia_shanghai_now() -> datetime:
    """获取当前的上海时间"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


@lru_cache(maxsize=1)
def get_project_version() -> str:
    """获取项目版本号"""
    pyproject_content = Path("pyproject.toml").read_text(encoding="utf-8")
    project_data = tomllib.loads(pyproject_content)
    return project_data["project"]["version"]


HTTPX_CLIENT = AsyncClient(timeout=20)
"""全局HTTPX客户端单例"""


COPILOT_CLIENT = CopilotClient(
    connection=RuntimeConnection.for_inprocess(),
    client_info={
        "application_name": "kanade_bot",
        "application_version": get_project_version(),
    },
)
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


@driver.on_shutdown
async def clear_image_cache():
    p = get_plugin_config(KanadeConfig).image_cache_dir_path
    if p.exists() and p.is_dir():
        for f in p.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning(f"删除图片缓存文件 {f} 时发生错误: {e}")
