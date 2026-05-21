from nonebot.plugin import PluginMetadata

from . import service as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)
