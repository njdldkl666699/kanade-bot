from nonebot import require

require("crystal")

from nonebot.plugin import PluginMetadata

from . import gateway as gateway
from . import handler as handler
from .config import Config

__plugin_meta__ = PluginMetadata(
    name="tts",
    description="TTS语音合成：反向WebSocket网关接收GPT-SoVITS推理节点拨号",
    usage="/tts <文本> 或 /tts <语言id> <文本>（语言id缺省为auto）",
    config=Config,
)
