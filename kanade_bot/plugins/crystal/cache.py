from kanade_bot.utils.cache import UserDailyCache

from .config import cfg
from .enum import DaypartEnum

check_in_cache = UserDailyCache(set[DaypartEnum], cfg.cache_file_path)
