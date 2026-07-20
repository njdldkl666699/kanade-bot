from kanade_bot.utils.cache import UserDailyCache
from kanade_bot.utils.common import PlatformType

from .config import cfg

harvest_power_cache = UserDailyCache(float, cfg.power_cache_file_path)


def get_or_init_harvest_power(platform: PlatformType, user_id: str) -> float:
    """获取用户的采集体力，如果不存在则初始化为每日初始体力"""
    power = harvest_power_cache.get(platform, user_id)
    if power is None:
        power = cfg.daily_power
        harvest_power_cache.set(platform, user_id, power)
    return power
