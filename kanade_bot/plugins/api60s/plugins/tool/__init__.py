from nonebot.plugin import PluginMetadata

from . import service as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="60s API 🍱 实用功能",
    usage="",
    config=Config,
)
