from pathlib import Path

from nonebot import require

from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file


class Config(AttrDocModel):
    api60s_fun_cache_file: str = "cache.json"
    """Fun插件缓存数据文件路径"""

    @property
    def api60s_fun_cache_file_path(self) -> Path:
        """Fun插件缓存数据文件路径"""
        return get_plugin_cache_file(self.api60s_fun_cache_file)


ConfigRegistry.register_config_types(Config)
