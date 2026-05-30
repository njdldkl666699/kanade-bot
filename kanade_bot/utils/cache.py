from datetime import datetime
import json
from pathlib import Path
from typing import Generic, TypeVar

from nonebot import get_driver, logger, require

from kanade_bot.utils.common import PlatformType

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

C = TypeVar("C")


class UserDailyCache(Generic[C]):
    _console_cache: dict[str, C] = {}
    _onebot_cache: dict[str, C] = {}

    @classmethod
    def get(cls, platform: PlatformType, user_id: str) -> C | None:
        if platform == "console":
            return cls._console_cache.get(user_id)
        elif platform == "onebot":
            return cls._onebot_cache.get(user_id)

    @classmethod
    def set(cls, platform: PlatformType, user_id: str, value: C) -> None:
        if platform == "console":
            cls._console_cache[user_id] = value
        elif platform == "onebot":
            cls._onebot_cache[user_id] = value

    @classmethod
    def _auto_clear_cache(cls):
        """每天凌晨自动清除缓存"""
        cls._console_cache.clear()
        cls._onebot_cache.clear()

    @classmethod
    def enable_auto_clear(cls):
        """启用每天自动清除缓存的功能"""
        scheduler.add_job(cls._auto_clear_cache, "cron", hour=0, minute=0)
        logger.info(f"已启用 {cls} 的每天自动清除功能")

    @classmethod
    def enable_persistence(cls, p: Path):
        """启用持久化功能，自动从文件加载和保存缓存数据"""
        driver = get_driver()

        @driver.on_startup
        def _():
            if not p.exists() or not p.is_file():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch()
                return

            data = json.load(p.open("r", encoding="utf-8"))

            cache_datetime = datetime.fromisoformat(data.get("datetime", "1970-01-01T00:00:00"))
            if cache_datetime.date() != datetime.now().date():
                logger.info(f"缓存数据已过期，日期为 {cache_datetime.date()}，已忽略")
                return

            cls._console_cache = data.get("console", {})
            cls._onebot_cache = data.get("onebot", {})
            logger.info(
                f"从 {p} 加载缓存数据，console: {len(cls._console_cache)} 条，onebot: {len(cls._onebot_cache)} 条"
            )

        @driver.on_shutdown
        def _():
            data = {
                "console": cls._console_cache,
                "onebot": cls._onebot_cache,
                "datetime": datetime.now().isoformat(),
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            json.dump(
                data,
                p.open("w", encoding="utf-8"),
                ensure_ascii=False,
                indent=2,
            )
            logger.info(
                f"已将缓存数据保存到 {p}，console: {len(cls._console_cache)} 条，onebot: {len(cls._onebot_cache)} 条"
            )
