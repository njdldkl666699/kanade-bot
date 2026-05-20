from nonebot import on_command
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="fun",
    description="60s API 🎤 消遣娱乐",
    usage="",
    config=Config,
)


hitokoto = on_command(
    "随机一言",
    aliases={"hitokoto", "一言"},
    priority=2,
    block=True,
)


luck = on_command(
    "今日运势",
    aliases={"luck", "运势"},
    priority=2,
    block=True,
)


fabing = on_command(
    "随机发病",
    aliases={"fabing", "发病"},
    priority=2,
    block=True,
)


answer = on_command(
    "随机答案之书",
    aliases={"答案之书", "随机答案", "bookofanswers"},
    priority=2,
    block=True,
)


kfc = on_command(
    "随机KFC文案",
    aliases={"KFC", "kfc", "肯德基", "疯狂星期四", "疯四", "v50", "v我50"},
    priority=2,
    block=True,
)


dad_joke = on_command(
    "随机冷笑话",
    aliases={"冷笑话", "dadjoke", "dad_joke"},
    priority=2,
    block=True,
)


__all__ = [
    "hitokoto",
    "luck",
    "fabing",
    "answer",
    "kfc",
    "dad_joke",
]
