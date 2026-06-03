from pathlib import Path

from nonebot import require

from kanade_bot.plugins import api60s

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file


class Config(api60s.Config):
    api60s_fun_cache_file: str = "cache.json"
    """Fun插件缓存数据文件路径"""

    @property
    def api60s_fun_cache_file_path(self) -> Path:
        """Fun插件缓存数据文件路径"""
        return get_plugin_cache_file(self.api60s_fun_cache_file)
