from nonebot.plugin import PluginMetadata

from . import service as _  # noqa: F401
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="60s API 🎤 消遣娱乐",
    usage="",
    config=Config,
)
