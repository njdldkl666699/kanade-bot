from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="summary",
    description="聊天记录总结",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config).summary


summarize = on_command(
    "总结",
    aliases={"summarize", "summary"},
    priority=2,
    block=True,
)


__all__ = ["summarize"]
