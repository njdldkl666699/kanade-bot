from nonebot import on_command, on_notice
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)


help_command = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)


offline_notice = on_notice(
    priority=1,
    block=False,
)


__all__ = ["help_command", "offline_notice"]
