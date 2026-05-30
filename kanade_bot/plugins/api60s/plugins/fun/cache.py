from nonebot import get_plugin_config
from pydantic import BaseModel

from kanade_bot.utils.cache import UserDailyCache

from .config import Config

cfg = get_plugin_config(Config)


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


UserDailyLuckCache = UserDailyCache[Luck]
UserDailyLuckCache.enable_auto_clear()
UserDailyLuckCache.enable_persistence(cfg.api60s_fun_cache_file_path)
