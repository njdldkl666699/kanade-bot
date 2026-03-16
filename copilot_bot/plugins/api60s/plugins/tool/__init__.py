from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from copilot_bot.plugins.api60s.client import client
from copilot_bot.plugins.argparser import parse_arg_message

from .cache import TranslateLang, TranslateLangCache
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="",
    usage="",
    config=Config,
)


weather = on_command(
    "实时天气",
    aliases={"天气", "weather"},
    priority=2,
    block=True,
    force_whitespace=True,
)


@weather.handle()
async def handle_weather(args: Message = CommandArg()):
    response = await client.get(
        "/v2/weather",
        params={
            "query": args.extract_plain_text(),
            "encoding": "text",
        },
    )
    await weather.finish(response.text)


weather_forecast = on_command(
    "天气预报",
    aliases={"weather_forecast"},
    priority=2,
    block=True,
)


@weather_forecast.handle()
async def handle_weather_forecast(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"query": str, "days": int})
    response = await client.get(
        "/v2/weather/forecast",
        params={
            "query": args["query"],
            "days": args["days"],
            "encoding": "text",
        },
    )
    await weather_forecast.finish(response.text)


fanyi = on_command(
    "翻译",
    aliases={"fanyi", "translate"},
    priority=2,
    block=True,
)


def format_langs(langs: list[TranslateLang]) -> str:
    if not langs:
        return "未查询到匹配语言。"

    lines = ["查询到以下语言："]
    for lang in langs:
        lines.append(f"- {lang.label} ({lang.code})")
    return "\n".join(lines)


@fanyi.handle()
async def handle_fanyi(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"text": str, "to_query": str, "from_query": str})
    text, to_query, from_query = args["text"], args["to_query"], args["from_query"]

    if not text:
        await fanyi.finish("请输入要翻译的文本。")

    to_matches = await TranslateLangCache.query_langs(to_query)
    if to_query is None or to_query.lower() == "auto":
        to_lang = "auto"
    elif len(to_matches) == 1:
        to_lang = to_matches[0].code
    else:
        await fanyi.finish(format_langs(to_matches))

    from_matches = await TranslateLangCache.query_langs(from_query)
    if from_query is None or from_query.lower() == "auto":
        from_lang = "auto"
    elif len(from_matches) == 1:
        from_lang = from_matches[0].code
    else:
        await fanyi.finish(format_langs(from_matches))

    response = await client.get(
        "/v2/fanyi",
        params={
            "text": text,
            "from": from_lang,
            "to": to_lang,
            "encoding": "text",
        },
    )
    await fanyi.finish(response.text)


moyu = on_command(
    "摸鱼日报",
    aliases={"摸鱼", "moyu"},
    priority=2,
    block=True,
)


@moyu.handle()
async def handle_moyu():
    response = await client.get("/v2/moyu", params={"encoding": "text"})
    await moyu.finish(response.text)
