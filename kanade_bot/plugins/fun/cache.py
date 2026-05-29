from pathlib import Path

from nonebot import get_plugin_config, require

from kanade_bot.utils.cache import UserDailyCache
from kanade_bot.utils.common import PlatformType

from .config import Config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file

cfg = get_plugin_config(Config).fun


class UserDailyWaifuCache(UserDailyCache[Path]):
    _cache: dict[str, Path] = {}
    """用户每日老婆缓存，键为用户ID，值为图片缓存路径"""

    @classmethod
    def get_bytes(cls, platform: PlatformType, user_id: str) -> bytes | None:
        """获取用户的老婆图片数据"""
        p = super().get(platform, user_id)
        if p and p.exists():
            return p.read_bytes()

    @classmethod
    def set_bytes(cls, platform: PlatformType, user_id: str, image: bytes) -> Path:
        """设置用户的老婆图片数据"""
        p = get_plugin_cache_file(f"{platform}-{user_id}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(image)
        cls._cache[user_id] = p
        return p

    @classmethod
    def delete(cls, user_id: str):
        """删除用户的老婆图片URL"""
        if user_id in cls._cache:
            del cls._cache[user_id]


UserDailyWaifuCache.enable_persistence(cfg.cache_file_path)
