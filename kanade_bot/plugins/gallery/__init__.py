from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gallery",
    description="画廊",
    usage="",
    config=Config,
)
