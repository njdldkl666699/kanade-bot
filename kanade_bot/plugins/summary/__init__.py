from nonebot.plugin import PluginMetadata

from . import service as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="summary",
    description="聊天记录总结",
    usage="",
    config=Config,
)
