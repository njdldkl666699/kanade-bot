from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel, NonNegativeInt, PositiveInt

from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_file


class MindleConfig(AttrDocModel):
    max_attempts: PositiveInt = 10
    """最大尝试次数"""
    crystal_bonus: NonNegativeInt = 0
    """猜出配方后的水晶奖励，若为 0 则不启用水晶奖励功能"""

    background_image: str = "background.png"
    """工作台背景图片文件名"""
    lang_file: str = "zh_cn.json"
    """Minecraft 中文语言文件名"""
    render_items_dir: str = "items/"
    """渲染物品贴图目录名"""
    recipes_dir: str = "recipe/"
    """Minecraft 配方数据目录名"""

    @property
    def background_image_path(self) -> Path:
        return get_plugin_config_file(self.background_image)

    @property
    def lang_file_path(self) -> Path:
        return get_plugin_config_file(self.lang_file)

    @property
    def render_items_dir_path(self) -> Path:
        return get_plugin_config_file(self.render_items_dir)

    @property
    def recipes_dir_path(self) -> Path:
        return get_plugin_config_file(self.recipes_dir)


class Config(BaseModel):
    mindle: MindleConfig = MindleConfig()
    """猜配方配置"""


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).mindle
