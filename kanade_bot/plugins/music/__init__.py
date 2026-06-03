from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="music",
    description="音乐相关功能",
    usage="",
    config=Config,
)
