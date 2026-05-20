from nonebot.adapters import Bot, Message
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.console.message import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11.message import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg
from nonebot_plugin_htmlrender import md_to_pic

from kanade_bot.plugins.api60s.client import client

from . import ai_news, daily60s, epic, it_news


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


@epic.handle()
async def handle_epic():
    response = await client.get(
        "/v2/epic",
        params={"encoding": "text"},
    )
    await epic.finish(response.text)


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
