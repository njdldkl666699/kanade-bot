from datetime import datetime
from pathlib import Path

from nonebot import get_driver, logger, require
from pydantic import BaseModel

from kanade_bot.utils.common import PlatformType

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler


class UserDailyCacheModel[T](BaseModel):
    """用户每日缓存的数据模型"""

    console: dict[str, T] = {}
    onebot: dict[str, T] = {}
    updated_at: datetime = datetime.now()

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


class UserDailyCache[T]:
    """用户每日缓存，支持 console 和 onebot 两个平台"""

    def __init__(self, T_type: type[T], persistence_file_path: Path):
        self._data = UserDailyCacheModel[T]()
        self._file_path = persistence_file_path

        # 每天自动清除缓存
        scheduler.add_job(self._auto_clear_cache, "cron", hour=0, minute=0)
        logger.debug(f"已启用 {self} 的每天自动清除功能")

        # 持久化路径
        p = self._file_path
        driver = get_driver()

        @driver.on_startup
        def _():
            if not p.exists() or not p.is_file():
                self._save()
                return

            data_json = p.read_text(encoding="utf-8")
            data = UserDailyCacheModel[T_type].model_validate_json(data_json)
            if data.updated_at.date() != datetime.now().date():
                logger.info(f"缓存数据已过期，日期为 {data.updated_at.date()}，已忽略")
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
        return self._data.get_by_platform(platform).get(user_id)

    def set(self, platform: PlatformType, user_id: str, value: T) -> None:
        cache = self._data.get_by_platform(platform)
        cache[user_id] = value
        self._save()

    def _auto_clear_cache(self):
        """每天凌晨自动清除缓存"""
        self._data = UserDailyCacheModel[T]()
        self._save()

    def _save(self):
        """将当前缓存数据保存到文件"""
        self._data.updated_at = datetime.now()
        self._file_path.parent.mkdir(parents=True, exist_ok=True)
        data_json = self._data.model_dump_json(ensure_ascii=False, indent=2)
        self._file_path.write_text(data_json, encoding="utf-8")
