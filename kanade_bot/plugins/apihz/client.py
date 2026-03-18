from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)

client = AsyncClient(
    base_url=cfg.apihz_api_url,
    params={"id": cfg.apihz_id, "key": cfg.apihz_key},
)

driver = get_driver()


@driver.on_startup
async def startup():
    logger.info("接口盒子 HTTP客户端已启动")


@driver.on_shutdown
async def shutdown():
    await client.aclose()
    logger.info("接口盒子 HTTP客户端已关闭")
