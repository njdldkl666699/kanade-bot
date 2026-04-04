from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)

client = AsyncClient(base_url=cfg.api60s_base_url)

driver = get_driver()


@driver.on_startup
def startup():
    logger.info("API60s HTTP客户端已启动")


@driver.on_shutdown
async def shutdown():
    await client.aclose()
    logger.info("API60s HTTP客户端已关闭")
