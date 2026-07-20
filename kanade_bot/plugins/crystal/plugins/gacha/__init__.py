from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="gacha",
    description="抽卡系统",
    usage="",
    config=Config,
)
