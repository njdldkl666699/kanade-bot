from nonebot.plugin import PluginMetadata

from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="猜配方",
    description="猜 Minecraft 工作台合成配方",
    usage="/mindle 或 /猜配方",
    config=Config,
)
