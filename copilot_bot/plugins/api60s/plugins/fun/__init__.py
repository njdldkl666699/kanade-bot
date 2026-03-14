import requests
from nonebot import get_plugin_config, on_command
from nonebot.adapters import Event, Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from copilot_bot.plugins.api60s.plugins.fun.cache import Luck, UserDailyLuckCache

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


hitokoto = on_command(
    "随机一言",
    aliases={"hitokoto", "一言"},
    priority=2,
    block=True,
)


@hitokoto.handle()
async def handle_hitokoto():
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/hitokoto",
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

    response = requests.get(
        f"{cfg.api60s_base_url}/v2/luck",
    )
    user_luck = Luck.model_validate(response.json())

    UserDailyLuckCache.set_user_luck_cache(event.get_user_id(), user_luck)
    await luck.finish(str(user_luck))


fabing = on_command(
    "随机发病",
    aliases={"fabing", "发病"},
    priority=2,
    block=True,
)


@fabing.handle()
async def handle_fabing(arg_msg: Message = CommandArg()):
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/fabing",
        params={
            "name": arg_msg.extract_plain_text(),
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
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/answer",
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
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/kfc",
        params={"encoding": "text"},
    )
    await kfc.finish(response.text)
