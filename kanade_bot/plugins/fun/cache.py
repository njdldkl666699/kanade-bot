from pathlib import Path

from nonebot import require

require("nonebot_plugin_apscheduler")
require("nonebot_plugin_localstore")

from nonebot_plugin_apscheduler import scheduler
from nonebot_plugin_localstore import get_plugin_cache_file


class UserDailyWaifuCache:
    _cache: dict[str, Path] = {}
    """用户每日老婆缓存，键为用户ID，值为图片缓存路径"""

    @classmethod
    def get(cls, user_id: str) -> bytes | None:
        """获取用户的老婆图片数据"""
        p = cls._cache.get(user_id)
        if p and p.exists():
            return p.read_bytes()

    @classmethod
    def get_path(cls, user_id: str) -> Path | None:
        """获取用户的老婆图片路径"""
        p = cls._cache.get(user_id)
        if p and p.exists():
            return p

    @classmethod
    def set(cls, user_id: str, image: bytes) -> Path:
        """设置用户的老婆图片数据"""
        p = get_plugin_cache_file(f"{user_id}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(image)
        cls._cache[user_id] = p
        return p

    @classmethod
    def delete(cls, user_id: str):
        """删除用户的老婆图片URL"""
        if user_id in cls._cache:
            del cls._cache[user_id]

    @staticmethod
    @scheduler.scheduled_job("cron", hour=0, minute=0)
    def _auto_clear_cache():
        """每天凌晨自动清除运势缓存"""
        UserDailyWaifuCache._cache.clear()
