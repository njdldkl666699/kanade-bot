import requests
from nonebot import get_plugin_config, on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from copilot_bot.plugins.api60s.argparser import parse_arg_message

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


weather = on_command(
    "实时天气",
    aliases={"天气", "weather"},
    priority=2,
    block=True,
    force_whitespace=True,
)


@weather.handle()
async def handle_weather(args: Message = CommandArg()):
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/weather",
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
    response = requests.get(
        f"{cfg.api60s_base_url}/v2/weather/forecast",
        params={
            "query": args["query"],
            "days": args["days"],
            "encoding": "text",
        },
    )
    await weather_forecast.finish(response.text)
