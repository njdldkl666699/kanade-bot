from typing import override

from nonebot import get_driver, logger

from kanade_bot.utils.cache import UserDailyCache, UserDailyCacheModel
from kanade_bot.utils.common import PlatformType

from .config import cfg


class HarvestPowerCache(UserDailyCache[float]):
    """采集体力缓存，扩展支持持久化的体力恢复"""

    def __init__(self):
        super().__init__(float, cfg.power_cache_file_path)
        self._crystal_power_data = UserDailyCacheModel[float]()
        """来自水晶恢复的体力数据，不会被每日清除"""

        # 持久化路径
        p = cfg.crystal_power_cache_file_path
        driver = get_driver()

        @driver.on_startup
        def _():
            if not p.exists() or not p.is_file():
                self._save()
                return

            c_data_json = p.read_text(encoding="utf-8")
            c_data = UserDailyCacheModel[float].model_validate_json(c_data_json)
            self._crystal_power_data = c_data
            logger.info(
                "从 {} 加载缓存数据，console: {} 条，onebot: {} 条",
                p,
                len(c_data.console),
                len(c_data.onebot),
            )

        @driver.on_shutdown
        def _():
            self._save()
            logger.info(
                "已将缓存数据保存到 {}，console: {} 条，onebot: {} 条",
                p,
                len(self._crystal_power_data.console),
                len(self._crystal_power_data.onebot),
            )

    @override
    def get(self, platform: PlatformType, user_id: str) -> float:
        """获取用户的采集体力"""
        crystal_power = self._crystal_power_data.get_by_platform(platform).get(user_id, 0)
        power = super().get(platform, user_id)
        if power is None:
            # 如果用户没有每日赠送体力，则初始化为每日初始体力
            power = cfg.daily_power
            super().set(platform, user_id, power)
        return power + crystal_power

    def set_by(self, platform: PlatformType, user_id: str, value: float) -> None:
        """修改用户的采集体力，value为变化量，优先修改水晶恢复的体力"""
        if value == 0:
            return

        crystal_power = self._crystal_power_data.get_by_platform(platform).get(user_id, 0)
        new_crystal_power = crystal_power + value
        value = 0
        if new_crystal_power < 0:
            # 如果水晶恢复的体力不足以抵消变化量
            # 则将剩余的变化量应用到每日赠送的体力上
            value = new_crystal_power
            new_crystal_power = 0
        self._crystal_power_data.get_by_platform(platform)[user_id] = new_crystal_power

        if value < 0:
            power = super().get(platform, user_id)
            if power is None:
                # 如果用户没有每日赠送体力，则初始化为每日初始体力
                power = cfg.daily_power
            new_power = power + value
            self._data.get_by_platform(platform)[user_id] = new_power

        self._save()

    @override
    def _save(self):
        """将当前缓存数据保存到文件"""
        super()._save()
        cd = self._crystal_power_data
        cp = cfg.crystal_power_cache_file_path
        cd.updated_at = self._data.updated_at
        crystal_data_json = cd.model_dump_json(indent=2, ensure_ascii=False)
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(crystal_data_json, encoding="utf-8")


harvest_power_cache = HarvestPowerCache()
