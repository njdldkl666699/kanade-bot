from datetime import datetime
from pathlib import Path

from nonebot import get_driver, logger, require
from pydantic import BaseModel

from kanade_bot.utils.cache import UserDailyCache
from kanade_bot.utils.common import PlatformType

from .config import cfg
from .enum import DaypartEnum

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

check_in_cache = UserDailyCache(set[DaypartEnum], cfg.cache_file_path)


class UserWeeklyCacheModel[T](BaseModel):
    """用户每周缓存的数据模型

    dict[str, dict[int, T]] 的key为用户ID；其中
    dict[int, T] 的key为isoweekday（1=周一, 7=周日），value为缓存数据
    """

    console: dict[str, dict[int, T]] = {}
    onebot: dict[str, dict[int, T]] = {}
    updated_at: datetime = datetime.now()

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


class UserWeeklyCache[T]:
    """用户每周缓存，支持 console 和 onebot 两个平台"""

    def __init__(self, T_type: type[T], persistence_file_path: Path):
        self._data = UserWeeklyCacheModel[T]()
        self._file_path = persistence_file_path

        # 每周自动清除缓存
        scheduler.add_job(self._auto_clear_cache, "cron", day_of_week="mon", hour=0, minute=0)
        logger.debug(f"已启用 {self} 的每周自动清除功能")

        # 持久化路径
        p = self._file_path
        driver = get_driver()

        @driver.on_startup
        def _():
            if not p.exists() or not p.is_file():
                self._save()
                return

            data_json = p.read_text(encoding="utf-8")
            data = UserWeeklyCacheModel[T_type].model_validate_json(data_json)
            # 如果周数不同，则清空缓存
            data_week = data.updated_at.isocalendar()[1]
            current_week = datetime.now().isocalendar()[1]
            if data_week != current_week:
                logger.info(
                    "缓存数据已过期，日期为 {}，周数为 {}，当前周数为 {}，已忽略",
                    data.updated_at.date(),
                    data_week,
                    current_week,
                )
                return

            self._data = data
            logger.info(
                "从 {} 加载缓存数据，console: {} 条，onebot: {} 条",
                p,
                len(self._data.console),
                len(self._data.onebot),
            )

        @driver.on_shutdown
        def _():
            self._save()
            logger.info(
                "已将缓存数据保存到 {}，console: {} 条，onebot: {} 条",
                p,
                len(self._data.console),
                len(self._data.onebot),
            )

    def get(self, platform: PlatformType, user_id: str) -> T | None:
        weekday = datetime.now().isoweekday()  # 获取当前日期的星期几（1=周一, 7=周日）
        return self._data.get_by_platform(platform).get(user_id, {}).get(weekday)

    def get_week(self, platform: PlatformType, user_id: str) -> dict[int, T] | None:
        """获取用户本周的缓存数据，返回一个字典，键为isoweekday（1=周一, 7=周日），值为缓存数据"""
        return self._data.get_by_platform(platform).get(user_id)

    def set(self, platform: PlatformType, user_id: str, value: T) -> None:
        weekday = datetime.now().isoweekday()
        cache = self._data.get_by_platform(platform)
        cache[user_id] = cache.get(user_id, {})
        cache[user_id][weekday] = value
        self._save()

    def _auto_clear_cache(self):
        """每周凌晨自动清除缓存"""
        self._data = UserWeeklyCacheModel[T]()
        self._save()

    def _save(self):
        """将当前缓存数据保存到文件"""
        self._data.updated_at = datetime.now()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        data_json = self._data.model_dump_json(ensure_ascii=False, indent=2)
        self._file_path.write_text(data_json, encoding="utf-8")


check_in_weekly_cache = UserWeeklyCache(bool, cfg.weekly_check_in_file_path)
