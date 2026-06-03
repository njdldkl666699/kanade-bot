from pathlib import Path

from nonebot import get_plugin_config, require

from kanade_bot.utils.cache import UserDailyCache
from kanade_bot.utils.common import PlatformType

from .config import Config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file

cfg = get_plugin_config(Config).fun


class UserDailyWaifuCache(UserDailyCache[Path]):
    """用户每日老婆图片缓存，存储用户的老婆图片文件路径"""

    def get_bytes(self, platform: PlatformType, user_id: str) -> bytes | None:
        """获取用户的老婆图片数据"""
        p = self.get(platform, user_id)
        if p and p.exists():
            return p.read_bytes()

    def set_bytes(self, platform: PlatformType, user_id: str, image: bytes) -> Path:
        """设置用户的老婆图片数据"""
        p = get_plugin_cache_file(f"{platform}-{user_id}.png")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(image)
        self.set(platform, user_id, p)
        return p

    def delete(self, user_id: str):
        """删除用户的老婆图片路径，并删除文件缓存"""
        if user_id in self._data.console_cache:
            p = self._data.console_cache.pop(user_id)
            if p and p.exists():
                p.unlink()
        if user_id in self._data.onebot_cache:
            p = self._data.onebot_cache.pop(user_id)
            if p and p.exists():
                p.unlink()


waifuCache = UserDailyWaifuCache(cfg.cache_file_path)
