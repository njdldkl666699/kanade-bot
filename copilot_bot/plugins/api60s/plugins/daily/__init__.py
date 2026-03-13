import requests
from nonebot import get_plugin_config, on_command
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.message import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11.message import MessageSegment as OneBotMessageSegment
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="daily",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)


daily60s = on_command(
    "每天60秒读懂世界", rule=to_me(), priority=1, aliases={"60s", "60秒"}, block=True
)


@daily60s.handle()
async def handle_60s_console(bot: ConsoleBot):
    response = requests.get(
        f"{config.api60s_base_url}/v2/60s",  # type: ignore
        params={"encoding": "markdown"},
    )
    await daily60s.finish(ConsoleMessageSegment.markdown(response.text))


@daily60s.handle()
async def handle_60s_onebot(bot: OneBot):
    response = requests.get(
        f"{config.api60s_base_url}/v2/60s",  # type: ignore
        params={"encoding": "image"},
    )
    await daily60s.finish(OneBotMessageSegment.image(response.content))
