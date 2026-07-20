from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="harvest",
    description="采集系统",
    usage="",
    config=Config,
)
