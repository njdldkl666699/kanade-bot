from pathlib import Path

from nonebot import get_plugin_config, require
from nonebot.adapters.console import Message as ConsoleMessage
from nonebot.adapters.onebot.v11 import Message as OneBotMessage

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
        if user_id in self._data.console:
            p = self._data.console.pop(user_id)
            if p and p.exists():
                p.unlink()
        if user_id in self._data.onebot:
            p = self._data.onebot.pop(user_id)
            if p and p.exists():
                p.unlink()


waifu_cache = UserDailyWaifuCache(Path, cfg.cache_file_path)


class GroupMessageCache:
    """群消息缓存，存储群内的消息列表"""

    def __init__(self):
        self._console: dict[str, list[ConsoleMessage]] = {}
        self._onebot: dict[int, list[OneBotMessage]] = {}

    def get_console(self, group_id: str) -> list[ConsoleMessage]:
        """获取群消息列表，如果不存在则创建一个空列表"""
        if group_id not in self._console:
            self._console[group_id] = []
        return self._console[group_id]

    def get_onebot(self, group_id: int) -> list[OneBotMessage]:
        """获取群消息列表，如果不存在则创建一个空列表"""
        if group_id not in self._onebot:
            self._onebot[group_id] = []
        return self._onebot[group_id]


group_message_cache = GroupMessageCache()
