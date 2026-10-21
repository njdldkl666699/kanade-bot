from pathlib import Path

import nonebot
from httpx import HTTPError
from nonebot.matcher import Matcher
from nonebot.message import run_postprocessor
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="api60s",
    description="⏰ 60s API 插件，接入60s API 提供的部分接口",
    usage="",
    config=Config,
)

sub_plugins = nonebot.load_plugins(str(Path(__file__).parent.joinpath("plugins").resolve()))


@run_postprocessor
async def _(matcher: Matcher, exception: Exception | None):
    if (
        (id := matcher.plugin_id)
        and id.startswith("api60s")
        and exception
        and isinstance(exception, HTTPError)
    ):
        nonebot.logger.warning(f"请求失败：{exception}")
        await matcher.finish(f"请求失败：{exception}")
