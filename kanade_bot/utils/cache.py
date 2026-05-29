import json
from pathlib import Path
from typing import Generic, TypeVar

from nonebot import get_driver, require

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

    @scheduler.scheduled_job("cron", hour=0, minute=0)
    @classmethod
    def _auto_clear_cache(cls):
        """每天凌晨自动清除缓存"""
        cls._console_cache.clear()
        cls._onebot_cache.clear()

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
            cls._console_cache = data.get("console", {})
            cls._onebot_cache = data.get("onebot", {})

        @driver.on_shutdown
        def _():
            data = {
                "console": cls._console_cache,
                "onebot": cls._onebot_cache,
            }
            p.parent.mkdir(parents=True, exist_ok=True)
            json.dump(
                data,
                p.open("w", encoding="utf-8"),
                ensure_ascii=False,
                indent=2,
            )
