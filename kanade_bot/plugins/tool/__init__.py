from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="实用工具和OneBot扩展功能",
    usage="",
    config=Config,
)
