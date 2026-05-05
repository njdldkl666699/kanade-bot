from pathlib import Path

import nonebot
from httpx import TimeoutException
from nonebot import get_plugin_config
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="api60s",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

sub_plugins = nonebot.load_plugins(str(Path(__file__).parent.joinpath("plugins").resolve()))


@run_postprocessor
async def do_something(matcher: Matcher, exception: Exception | None):
    match exception:
        case TimeoutException():
            await matcher.finish("请求超时，请稍后再试")
