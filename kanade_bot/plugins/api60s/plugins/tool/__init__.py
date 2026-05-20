from nonebot import on_command
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="60s API 🍱 实用功能",
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


weather_forecast = on_command(
    "天气预报",
    aliases={"weather_forecast"},
    priority=2,
    block=True,
)


fanyi = on_command(
    "翻译",
    aliases={"fanyi", "translate"},
    priority=2,
    block=True,
)


moyu = on_command(
    "摸鱼日报",
    aliases={"摸鱼", "moyu"},
    priority=2,
    block=True,
)


__all__ = ["weather", "weather_forecast", "fanyi", "moyu"]
