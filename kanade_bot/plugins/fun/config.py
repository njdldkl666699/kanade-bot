from pathlib import Path

from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import (
    get_plugin_config_file,
    get_plugin_data_file,
    get_plugin_cache_file,
)


class ScopedConfig(BaseModel):
    ciallo_image_file: str = "宵崎奏Ciallo.webp"
    """Ciallo表情图片名"""
    plus_one_threshold: int = 0
    """+1功能的触发阈值，单位为条消息，小于等于0表示禁用"""
    duanzi_list_file: str = "duanzi.json"
    """段子(史)JSON列表文件名"""
    lolicon_proxy: str = "i.yuki.sh"
    """Lolicon API 图片代理"""
    cache_file: str = "cache.json"
    """缓存数据文件路径"""

    @property
    def ciallo_image_file_path(self) -> Path:
        """Ciallo表情图片文件路径"""
        return get_plugin_config_file(self.ciallo_image_file)

    @property
    def duanzi_list_file_path(self) -> Path:
        """段子(史)JSON列表文件路径"""
        return get_plugin_data_file(self.duanzi_list_file)

    @property
    def cache_file_path(self) -> Path:
        """缓存数据文件路径"""
        return get_plugin_cache_file(self.cache_file)


class Config(BaseModel):
    fun: ScopedConfig
