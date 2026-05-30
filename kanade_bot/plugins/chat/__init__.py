from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="AI聊天相关功能插件",
    usage="",
    config=Config,
)
