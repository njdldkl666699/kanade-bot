from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata

from .config import PROJECT_VERSION, Config

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


help = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)


@help.handle()
async def handle_help():
    await help.finish("宵崎奏Bot 帮助文档链接：\n" + cfg.help_link)


version = on_command(
    "Kanade版本",
    aliases={"kanade_version"},
    priority=2,
    block=True,
)


@version.handle()
async def handle_version():
    await version.finish("宵崎奏Bot 版本: " + PROJECT_VERSION)
