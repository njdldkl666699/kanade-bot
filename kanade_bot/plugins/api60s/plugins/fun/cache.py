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
    __cache: dict[str, Luck] = {}

    @staticmethod
    def get_user_luck_cache(user_id: str) -> Luck | None:
        """获取用户的运势数据"""
        return UserDailyLuckCache.__cache.get(user_id)

    @staticmethod
    def set_user_luck_cache(user_id: str, luck: Luck):
        """设置用户的运势数据"""
        UserDailyLuckCache.__cache[user_id] = luck

    @staticmethod
    @scheduler.scheduled_job("cron", hour=0, minute=0)
    def _auto_clear_cache():
        """每天凌晨自动清除运势缓存"""
        UserDailyLuckCache.__cache.clear()
