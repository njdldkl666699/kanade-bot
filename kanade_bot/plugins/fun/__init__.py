from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="各种娱乐功能",
    usage="",
    config=Config,
)
