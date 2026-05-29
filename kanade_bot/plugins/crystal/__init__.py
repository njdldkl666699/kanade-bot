from nonebot import get_plugin_config
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="crystal",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)
