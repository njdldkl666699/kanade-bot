import tomllib
from pathlib import Path

from nonebot import get_plugin_config, on_command
from nonebot.plugin import PluginMetadata

from .config import Config

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
    pyproject_content = Path("pyproject.toml").read_text(encoding="utf-8")
    project_data = tomllib.loads(pyproject_content)
    project_version = project_data["project"]["version"]
    await version.finish(f"宵崎奏Bot 版本: v{project_version}")
