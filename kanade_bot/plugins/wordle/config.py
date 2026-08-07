from nonebot import get_plugin_config
from pydantic import BaseModel

from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry


class ScopedConfig(AttrDocModel):
    default_length: int = 5
    """默认单词长度"""
    default_dictionary: str = "CET4"
    """默认词典"""

    min_length: int = 3
    """单词最小长度"""
    max_length: int = 13
    """单词最大长度"""

    meanings: set[str] = {"中释"}
    """单词释义类型，根据数据源可以选择多种释义类型"""

    crystal_bonus_map: dict[int, int] = {}
    """猜出单词后获得的水晶奖励，key为单词长度，value为奖励水晶数。
    
    如果未配置或配置为空字典，则不启用水晶奖励功能。
    """
    hinted_crystal_bonus_map: dict[int, int] = {}
    """使用提示后猜出单词获得的水晶奖励"""


class Config(BaseModel):
    wordle: ScopedConfig = ScopedConfig()


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).wordle
