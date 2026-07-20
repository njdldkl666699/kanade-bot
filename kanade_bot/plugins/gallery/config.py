from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel

require("nonebot_plugin_localstore")
require("model_updater")

from nonebot_plugin_localstore import get_plugin_data_dir, get_plugin_data_file

from kanade_bot.plugins.model_updater import load_register_model_from_file


class ScopedConfig(BaseModel):
    name_data_file: str = "gallery_name_indices.json"
    """画廊的名称数据文件名"""

    send_pic_limit: int = 10
    """每次发送图片的数量限制"""

    @property
    def name_data_file_path(self) -> Path:
        return get_plugin_data_file(self.name_data_file)

    @property
    def data_dir_path(self) -> Path:
        """画廊数据存储根目录"""
        return get_plugin_data_dir()


class Config(BaseModel):
    gallery: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).gallery


class GalleryNameData(BaseModel):
    """画廊数据

    画廊名称唯一，且与目录名一致；画廊别名不能重复
    """

    alias_to_name: dict[str, str] = {}
    """画廊别名到名称的映射"""

    name_to_aliases: dict[str, list[str]] = {}
    """画廊名称到别名的映射"""

    iota: int = 0
    """用于生成图片名称的自增计数器"""


gallery_name_data = load_register_model_from_file(GalleryNameData, cfg.name_data_file_path)
