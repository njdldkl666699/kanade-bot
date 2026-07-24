from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="60s API 🍱 实用功能",
    usage="",
    config=Config,
)
