from nonebot import require

from kanade_bot.plugins.api60s.client import client

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler


class Daily60sCache:
    """每天60秒读懂世界图片的内存缓存"""

    def __init__(self):
        self._image: bytes | None = None

    def get(self) -> bytes | None:
        """获取缓存的图片，未缓存时返回 None"""
        return self._image

    async def refresh(self) -> bytes:
        """删除旧缓存并重新请求，失败时缓存保持为空"""
        self._image = None
        r = await client.get("/v2/60s", params={"encoding": "image-proxy"})
        r.raise_for_status()
        self._image = r.content
        return self._image


daily60s_cache = Daily60sCache()

# 每天 06:00（Asia/Shanghai）预缓存当日图片
scheduler.add_job(
    daily60s_cache.refresh,
    "cron",
    hour=6,
    minute=0,
    timezone="Asia/Shanghai",
)
