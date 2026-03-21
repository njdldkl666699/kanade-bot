from nonebot import on_command
from nonebot.adapters import Event, Message
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from kanade_bot.plugins.api60s.client import client
from kanade_bot.plugins.api60s.plugins.fun.cache import Luck, UserDailyLuckCache

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="",
    usage="",
    config=Config,
)


hitokoto = on_command(
    "随机一言",
    aliases={"hitokoto", "一言"},
    priority=2,
    block=True,
)


@hitokoto.handle()
async def handle_hitokoto():
    response = await client.get(
        "/v2/hitokoto",
        params={"encoding": "text"},
    )
    await hitokoto.finish(response.text)


luck = on_command(
    "今日运势",
    aliases={"luck", "运势"},
    priority=2,
    block=True,
)


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


fabing = on_command(
    "随机发病",
    aliases={"fabing", "发病"},
    priority=2,
    block=True,
)


def resolve_nickname(event: Event) -> str | None:
    nickname: str | None = None

    # 处理OneBot消息事件
    if isinstance(event, OneBotMessageEvent):
        nickname = event.sender.nickname
    if isinstance(event, OneBotGroupMessageEvent):
        nickname = event.sender.card or event.sender.nickname

    # Console的消息事件
    if isinstance(event, ConsoleMessageEvent):
        nickname = event.user.nickname

    return nickname


@fabing.handle()
async def handle_fabing(event: Event, arg_msg: Message = CommandArg()):
    nickname = resolve_nickname(event) or arg_msg.extract_plain_text().strip()
    response = await client.get(
        "/v2/fabing",
        params={
            "name": nickname,
            "encoding": "text",
        },
    )
    await fabing.finish(response.text)


answer = on_command(
    "随机答案之书",
    aliases={"答案之书", "随机答案", "bookofanswers"},
    priority=2,
    block=True,
)


@answer.handle()
async def handle_answer():
    response = await client.get(
        "/v2/answer",
        params={"encoding": "text"},
    )
    await answer.finish(response.text)


kfc = on_command(
    "随机KFC文案",
    aliases={"KFC", "kfc", "肯德基", "疯狂星期四", "疯四", "v50", "v我50"},
    priority=2,
    block=True,
)


@kfc.handle()
async def handle_kfc():
    response = await client.get(
        "/v2/kfc",
        params={"encoding": "text"},
    )
    await kfc.finish(response.text)


dad_joke = on_command(
    "随机冷笑话",
    aliases={"冷笑话", "dadjoke", "dad_joke"},
    priority=2,
    block=True,
)


@dad_joke.handle()
async def handle_dad_joke():
    response = await client.get(
        "/v2/dad-joke",
        params={"encoding": "text"},
    )
    await dad_joke.finish(response.text)
