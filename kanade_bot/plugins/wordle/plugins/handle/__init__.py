from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="猜成语",
    description="汉字Wordle猜成语",
    usage="",
    config=Config,
)
