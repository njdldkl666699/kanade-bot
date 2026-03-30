from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)

file_client = AsyncClient()


tavily_client = AsyncClient(
    base_url="https://api.tavily.com",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.chat_tavily_api_key}",
    },
)

driver = get_driver()


@driver.on_startup
async def startup():
    logger.info("聊天文件读取 HTTP客户端已启动")
    logger.info("Tavily Search HTTP客户端已启动")


@driver.on_shutdown
async def shutdown():
    await file_client.aclose()
    logger.info("聊天文件读取 HTTP客户端已关闭")
    await tavily_client.aclose()
    logger.info("Tavily Search HTTP客户端已关闭")
