from nonebot import on_command, require

require("command_counter")
from kanade_bot.plugins.command_counter import register_matcher

tts_cmd = on_command(
    "tts",
    aliases={"TTS"},
    priority=2,
    block=True,
)
register_matcher(tts_cmd, "tts")
