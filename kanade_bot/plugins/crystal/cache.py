from kanade_bot.utils.cache import UserDailyCache

from .config import cfg

UserDailyCheckInCache = UserDailyCache[bool]
UserDailyCheckInCache.enable_auto_clear()
UserDailyCheckInCache.enable_persistence(cfg.cache_file_path)
