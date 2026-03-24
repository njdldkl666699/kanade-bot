from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)

client = AsyncClient()

driver = get_driver()


@driver.on_startup
async def startup():
    logger.info("聊天文件读取 HTTP客户端已启动")


@driver.on_shutdown
async def shutdown():
    await client.aclose()
    logger.info("聊天文件读取 HTTP客户端已关闭")
