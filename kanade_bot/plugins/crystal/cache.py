from kanade_bot.utils.cache import UserDailyCache

from .config import cfg

checkInCache = UserDailyCache[bool](cfg.cache_file_path)
