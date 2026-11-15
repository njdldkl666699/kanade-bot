import re

from nonebot import logger
from nonebot.adapters import Event, Message
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg

from kanade_bot.plugins.crystal import (
    HandlerKeyEnum,
    check_user_crystal_maybe_init,
    finish_fail_consume,
    succeed_consume,
)
from kanade_bot.utils.common import get_platform_type

from .config import TTS_DEFAULT_EMOTION, tts_configs
from .gateway import V2_LANGUAGES, TTSError, synthesize
from .matcher import tts_cmd


def parse_tts_args(raw: str) -> tuple[str, str]:
    """解析命令参数：首段为合法语言id时视为语言，否则语言auto、整体为文本"""
    parts = re.split(r"\s+", raw.strip(), maxsplit=1)
    if len(parts) == 2 and parts[0].lower() in V2_LANGUAGES:
        return parts[0].lower(), parts[1].strip()
    return "auto", raw.strip()


@tts_cmd.handle()
async def _(event: Event, arg: Message = CommandArg()):
    if not isinstance(event, OneBotEvent):
        await tts_cmd.finish("当前环境不支持发送语音，请在QQ消息环境中使用。")

    key = HandlerKeyEnum.TTS
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    raw_text = arg.extract_plain_text().strip()
    if not raw_text:
        await tts_cmd.finish(
            "用法：/tts <文本> 或 /tts <语言id> <文本>；语言id缺省为auto，如 /tts ja こんにちは"
        )
    text_lang, text = parse_tts_args(raw_text)
    if not text:
        await tts_cmd.finish("只指定了语言id，还需要提供要合成的文本。")

    preset = tts_configs.instance.tts_emotions.get(TTS_DEFAULT_EMOTION)
    if preset is None:
        await tts_cmd.finish("未配置语音情绪映射，无法合成语音。")

    # 先检查余额（新号自动注册），合成成功后才扣费
    if not await check_user_crystal_maybe_init(tts_cmd, key, platform, user_id):
        await finish_fail_consume(tts_cmd, key, platform, user_id)

    try:
        audio = await synthesize(
            text,
            text_lang,
            ref_audio_path=preset.ref_audio_path,
            prompt_text=preset.prompt_text,
            prompt_lang=preset.prompt_lang,
        )
    except TTSError as e:
        logger.warning("/tts 语音合成失败: {}", e)
        await tts_cmd.finish(f"语音合成失败: {e}")

    succeed_consume(key, platform, user_id)
    await tts_cmd.finish(OneBotMessageSegment.record(audio))
