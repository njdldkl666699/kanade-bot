from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel, NonNegativeInt

from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_dir


class HandleConfig(AttrDocModel):
    strict_mode: bool = False
    """是否启用严格模式，开启后猜测的短语必须是成语"""
    color_enhance: bool = False
    """是否启用色彩增强模式"""

    crystal_bonus: NonNegativeInt = 0
    """猜出成语后获得的水晶奖励，若为0则不启用水晶奖励功能"""

    idiom_file: str = "idioms.txt"
    """成语文件名"""
    answer_file: str = "answers.json"
    """成语答案文件名，包含可猜的成语及其释义"""

    @property
    def idiom_file_path(self) -> Path:
        return get_plugin_config_dir() / self.idiom_file

    @property
    def answer_file_path(self) -> Path:
        return get_plugin_config_dir() / self.answer_file


class Config(BaseModel):
    handle: HandleConfig = HandleConfig()
    """猜成语配置"""


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).handle
