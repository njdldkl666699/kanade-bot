from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="猜单词",
    description="wordle猜单词游戏系列",
    usage="",
    config=Config,
)
