from nonebot import require

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler


class UserDailyWaifuCache:
    _cache: dict[str, str] = {}
    """用户每日老婆缓存，键为用户ID，值为图片URL"""

    @classmethod
    def get(cls, user_id: str) -> str | None:
        """获取用户的老婆图片URL"""
        return cls._cache.get(user_id)

    @classmethod
    def set(cls, user_id: str, url: str):
        """设置用户的老婆图片URL"""
        cls._cache[user_id] = url

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
