from kanade_bot.utils.cache import UserDailyCache

from .config import DaypartEnum, cfg

check_in_cache = UserDailyCache(set[DaypartEnum], cfg.cache_file_path)
