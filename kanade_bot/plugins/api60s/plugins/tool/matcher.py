from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

weather = on_command(
    "实时天气",
    aliases={"天气", "weather"},
    priority=2,
    block=True,
    force_whitespace=True,
)
register_matcher(weather, "实时天气")

weather_forecast = on_command(
    "天气预报",
    aliases={"weather_forecast"},
    priority=2,
    block=True,
)
register_matcher(weather_forecast, "天气预报")

fanyi = on_command(
    "翻译",
    aliases={"fanyi", "translate"},
    priority=2,
    block=True,
)
register_matcher(fanyi, "翻译")

moyu = on_command(
    "摸鱼日报",
    aliases={"摸鱼", "moyu"},
    priority=2,
    block=True,
)
register_matcher(moyu, "摸鱼日报")
