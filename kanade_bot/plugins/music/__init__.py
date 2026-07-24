from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="music",
    description="音乐相关功能",
    usage="",
    config=Config,
)
