from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel

from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.config import ensure_json_config, write_json_config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_data_file, get_plugin_cache_file


class ScopedConfig(BaseModel):
    config_file: str = "credit_config.json"
    """水晶配置文件路径"""
    data_file: str = "userdata.json"
    """用户数据文件路径"""
    cache_file: str = "cache.json"
    """缓存数据文件路径"""

    @property
    def config_file_path(self) -> Path:
        return get_plugin_data_file(self.config_file)

    @property
    def data_file_path(self) -> Path:
        return get_plugin_data_file(self.data_file)

    @property
    def cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.cache_file)


class Config(BaseModel):
    credit: ScopedConfig


cfg = get_plugin_config(Config).credit


class CrystalConfig(BaseModel):
    """水晶配置"""

    check_in_crystal_min: int = 10
    """签到获得的最少水晶"""
    check_in_crystal_max: int = 20
    """签到获得的最多水晶"""

    command_consumes: dict[str, int] = {}
    """每个命令消耗的水晶，键为唯一ID，值为消耗的水晶数"""

    check_in_success_templates: list[str] = [
        "嗯，签到了。获得 {crystal} 水晶…一点点积攒起来呢。",
        "签好了。今天有{crystal} 水晶入账…不多，但也算个小进展。",
        "啊…签到成功。+{crystal} 水晶。希望没弄错。",
    ]

    check_in_failure_templates: list[str] = [
        "…看了一下，今天已经签过了。现在水晶是 {total_crystal}。",
        "嗯…好像之前就签到了。当前水晶：{total_crystal}。",
    ]


crystal_config = ensure_json_config(cfg.config_file_path, CrystalConfig)


def write_crystal_config():
    """将水晶配置写入文件"""
    write_json_config(cfg.config_file_path, crystal_config)


class CrystalData(BaseModel):
    console: dict[str, int] = {}
    """Console适配器用户水晶数据，键为用户ID，值为水晶数"""
    onebot: dict[str, int] = {}
    """OneBot适配器用户水晶数据，键为用户ID，值为水晶数"""

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


crystal_data = ensure_json_config(cfg.data_file_path, CrystalData)


def write_crystal_data():
    """将水晶数据写入文件"""
    write_json_config(cfg.data_file_path, crystal_data)
