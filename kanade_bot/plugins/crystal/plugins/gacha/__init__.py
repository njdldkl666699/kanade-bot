from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gacha",
    description="抽卡系统",
    usage="",
    config=Config,
)
