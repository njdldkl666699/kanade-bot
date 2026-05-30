from enum import Enum
from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel

from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.config import ensure_json_config, write_json_config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import (
    get_plugin_cache_file,
    get_plugin_config_file,
    get_plugin_data_file,
)


class ScopedConfig(BaseModel):
    config_file: str = "crystal_config.json"
    """水晶配置文件路径"""
    data_file: str = "userdata.json"
    """用户数据文件路径"""
    cache_file: str = "cache.json"
    """缓存数据文件路径"""

    @property
    def config_file_path(self) -> Path:
        return get_plugin_config_file(self.config_file)

    @property
    def data_file_path(self) -> Path:
        return get_plugin_data_file(self.data_file)

    @property
    def cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.cache_file)


class Config(BaseModel):
    credit: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).credit


class HandlerKeyEnum(Enum):
    """命令处理函数的唯一ID枚举"""

    CHAT = "聊天"
    REFRESH_WAIFU = "刷新老婆"
    RANDOM_WAIFU = "随机图"
    SUMMARIZE = "总结"


class CrystalConfig(BaseModel):
    """水晶配置"""

    check_in_crystal_min: int = 100
    """签到获得的最少水晶"""
    check_in_crystal_max: int = 200
    """签到获得的最多水晶"""

    check_in_succeed_templates: list[str] = [
        "嗯，签到了。获得 {crystal} 水晶…一点点积攒起来呢。",
        "签好了。今天有{crystal} 水晶入账…不多，但也算个小进展。",
        "啊…签到成功。+{crystal} 水晶。希望没弄错。",
        "嗯…轻轻按一下。签到成功，+{crystal} 水晶。",
        "签到了。虽然不多…但每天坚持一下也挺好的。+{crystal} 水晶。",
        "啊…显示签到成功了。获得 {crystal} 水晶…先记着吧。",
        "好，签好了。今天也加 {crystal} 水晶…慢慢来。",
    ]

    check_in_failed_templates: list[str] = [
        "…看了一下，今天已经签过了。现在水晶是 {total_crystal}。",
        "嗯…好像之前就签到了。当前水晶：{total_crystal}。",
        "…签过了。现在水晶是 {total_crystal}。明天再继续吧。",
    ]

    handler_consumes: dict[HandlerKeyEnum, int] = {}
    """每个命令的处理函数消耗的水晶，键为唯一ID，值为消耗的水晶数"""

    handler_consume_failed_templates: list[str] = [
        "嗯…水晶好像不够。需要 {consume} 水晶…还差一些。",
        "啊…不行。当前水晶有{crystal}，需要 {consume} 水晶…有点可惜。",
        "系统提示说水晶不够…需要 {consume} 水晶。我这边也没办法跳过呢。",
        "啊…这个需要 {consume} 水晶。现在还不够，只有{crystal}…再攒几天吧。",
        "…看来用不了。需要 {consume} 水晶，现在有 {crystal}…慢慢来吧。",
        "嗯…水晶还差一点呢。需要{consume}水晶，现在只有{crystal}。抱歉，暂时用不了这个功能。",
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
