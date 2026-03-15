from pathlib import Path

from nonebot import get_plugin_config, on_command
from nonebot.adapters import Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.message import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11.message import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg
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
async def handle_help(bot: ConsoleBot):
    help_doc = Path(cfg.help_markdown_path).read_text(encoding="utf-8")
    await help.finish(ConsoleMessageSegment.markdown(help_doc))


@help.handle()
async def handle_help_onebot(bot: OneBot, arg_msg: Message = CommandArg()):
    theme = arg_msg.extract_plain_text().strip()
    if theme == "dark":
        image_path = cfg.help_image_dark_path
    else:
        image_path = cfg.help_image_light_path

    try:
        await help.finish(OneBotMessageSegment.image(Path(image_path)))
    except FileNotFoundError:
        help_doc = Path(cfg.help_markdown_path).read_text(encoding="utf-8")
        await help.finish(OneBotMessageSegment.text(help_doc))
