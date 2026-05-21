from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler


class Luck(BaseModel):
    """运势数据模型"""

    luck_desc: str
    luck_rank: int
    luck_tip: str
    luck_tip_index: int

    def __str__(self):
        return (
            f"🎯 今日运势\n"
            f"━━━━━━━━━━━━━━━━\n"
            f"🌟 运势指数：{self.luck_rank:+d}\n"
            f"📝 运势描述：{self.luck_desc}\n"
            f"💡 贴心提示：{self.luck_tip}\n"
        )


class UserDailyLuckCache:
    _cache: dict[str, Luck] = {}

    @classmethod
    def get(cls, user_id: str) -> Luck | None:
        """获取用户的运势数据"""
        return cls._cache.get(user_id)

    @classmethod
    def set(cls, user_id: str, luck: Luck):
        """设置用户的运势数据"""
        cls._cache[user_id] = luck

    @staticmethod
    @scheduler.scheduled_job("cron", hour=0, minute=0)
    def _auto_clear_cache():
        """每天凌晨自动清除运势缓存"""
        UserDailyLuckCache._cache.clear()
