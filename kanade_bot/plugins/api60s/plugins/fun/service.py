from nonebot.adapters import Event, Message
from nonebot.params import CommandArg

from kanade_bot.plugins.api60s.client import client

from . import answer, dad_joke, fabing, hitokoto, kfc, luck
from .cache import Luck, UserDailyLuckCache


@hitokoto.handle()
async def handle_hitokoto():
    response = await client.get(
        "/v2/hitokoto",
        params={"encoding": "text"},
    )
    await hitokoto.finish(response.text)


@luck.handle()
async def handle_luck(event: Event):
    cache_luck = UserDailyLuckCache.get_user_luck_cache(event.get_user_id())
    if cache_luck:
        await luck.finish(str(cache_luck))
        return

    response = await client.get("/v2/luck")
    user_luck = Luck.model_validate(response.json()["data"])

    UserDailyLuckCache.set_user_luck_cache(event.get_user_id(), user_luck)
    await luck.finish(str(user_luck))


@fabing.handle()
async def handle_fabing(arg_msg: Message = CommandArg()):
    response = await client.get(
        "/v2/fabing",
        params={
            "name": arg_msg.extract_plain_text(),
            "encoding": "text",
        },
    )
    await fabing.finish(response.text)


@answer.handle()
async def handle_answer():
    response = await client.get(
        "/v2/answer",
        params={"encoding": "text"},
    )
    await answer.finish(response.text)


@kfc.handle()
async def handle_kfc():
    response = await client.get(
        "/v2/kfc",
        params={"encoding": "text"},
    )
    await kfc.finish(response.text)


@dad_joke.handle()
async def handle_dad_joke():
    response = await client.get(
        "/v2/dad-joke",
        params={"encoding": "text"},
    )
    await dad_joke.finish(response.text)
