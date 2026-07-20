from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gallery",
    description="画廊",
    usage="",
    config=Config,
)
