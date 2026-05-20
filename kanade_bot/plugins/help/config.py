from pathlib import Path

from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import (
    get_plugin_cache_dir,
    get_plugin_config_dir,
    get_plugin_config_file,
)


class ScopedConfig(BaseModel):
    haruki_image_file: str = "Haruki_help.png"
    """Haruki的帮助图片名，不存在则不发送"""
    server_chan_turbo_key: str = ""
    """Server酱Turbo的Key，配置后会在Bot掉线时推送通知到微信"""
    online_notice_group_ids: list[int] = []
    """Bot上线通知的群聊ID列表，配置后会在Bot上线时发送通知到这些群聊"""

    @property
    def docs_dir_path(self) -> Path:
        """帮助文档所在目录路径"""
        return get_plugin_config_dir()

    @property
    def images_cache_dir_path(self) -> Path:
        """帮助文档图片的缓存目录路径"""
        return get_plugin_cache_dir()

    @property
    def haruki_image_file_path(self) -> Path:
        """Haruki的帮助图片文件路径"""
        return get_plugin_config_file(self.haruki_image_file)


class Config(BaseModel):
    help: ScopedConfig
