from nonebot import on_command
from nonebot.adapters import Bot, Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.message import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11.message import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot_plugin_htmlrender import md_to_pic

from ...client import client
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
    response = await client.get("/v2/60s")
    image_url = response.json()["data"]["image"]
    await daily60s.finish(OneBotMessageSegment.image(image_url))


ai_news = on_command(
    "AI资讯快报",
    aliases={"ai新闻", "AI资讯", "ai_news", "ai-news"},
    priority=2,
    block=True,
)


@ai_news.handle()
async def _(bot: Bot, arg_msg: Message = CommandArg()):
    date = arg_msg.extract_plain_text().strip()
    response = await client.get(
        "/v2/ai-news",
        params={
            "date": date,
            "encoding": "markdown",
        },
    )
    text = response.text
    if isinstance(bot, ConsoleBot):
        await ai_news.finish(ConsoleMessageSegment.markdown(text))
    elif isinstance(bot, OneBot):
        image = await md_to_pic(text)
        await ai_news.finish(OneBotMessageSegment.image(image))
    else:
        await ai_news.finish(text)


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


it_news = on_command(
    "实时IT资讯",
    aliases={"it新闻", "IT资讯", "it_news", "it-news"},
    priority=2,
    block=True,
)


@it_news.handle()
async def _(bot: Bot):
    response = await client.get(
        "/v2/it-news",
        params={
            "encoding": "markdown",
            "limit": 10,
        },
    )
    text = response.text
    if isinstance(bot, ConsoleBot):
        await it_news.finish(ConsoleMessageSegment.markdown(text))
    elif isinstance(bot, OneBot):
        image = await md_to_pic(text)
        await it_news.finish(OneBotMessageSegment.image(image))
    else:
        await it_news.finish(text)
