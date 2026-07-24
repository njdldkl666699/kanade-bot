from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="imgtool",
    description="图片处理",
    usage="",
    config=Config,
)
