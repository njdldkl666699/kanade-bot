from nonebot import on_command
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.message import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11.message import MessageSegment as OneBotMessageSegment
from nonebot.plugin import PluginMetadata

from copilot_bot.plugins.api60s.client import client

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="daily",
    description="",
    usage="",
    config=Config,
)


daily60s = on_command(
    "每天60秒读懂世界",
    priority=2,
    aliases={"60s", "60秒", "日报"},
    block=True,
)


@daily60s.handle()
async def handle_60s_console(bot: ConsoleBot):
    response = await client.get(
        "/v2/60s",
        params={"encoding": "markdown"},
    )
    await daily60s.finish(ConsoleMessageSegment.markdown(response.text))


@daily60s.handle()
async def handle_60s_onebot(bot: OneBot):
    response = await client.get(
        "/v2/60s",
        params={"encoding": "image"},
    )
    await daily60s.finish(OneBotMessageSegment.image(response.content))


epic = on_command(
    "Epic",
    aliases={"epic", "epic游戏"},
    priority=2,
    block=True,
)


@epic.handle()
async def handle_epic():
    response = await client.get(
        "/v2/epic",
        params={"encoding": "text"},
    )
    await epic.finish(response.text)
