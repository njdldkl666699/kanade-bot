from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="daily",
    description="60s API 🕘 周期资讯",
    usage="",
    config=Config,
)
