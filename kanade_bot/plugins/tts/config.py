"""TTS插件配置：网关静态参数与运行时情绪映射"""

from pathlib import Path
from typing import Final, Literal, Self, get_args

from nonebot import get_plugin_config, require
from pydantic import BaseModel, model_validator

from kanade_bot.utils.schema import AttrDocModel, ConfigRegistry, generate_schema

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file

require("model_updater")
from kanade_bot.plugins.model_updater import load_register_model_from_file

Emotion = Literal["平静", "快乐", "悲伤", "愤怒", "恐惧", "惊讶", "厌恶", "困惑"]
"""send_voice 工具的固定情绪枚举"""

TTS_EMOTIONS: Final[tuple[str, ...]] = get_args(Emotion)
"""情绪枚举全部取值（由 Emotion 派生）"""

TTS_DEFAULT_EMOTION: Final = "平静"
"""默认情绪：工具参数缺省值，其余情绪缺项配置时回退到它"""


class ScopedConfig(AttrDocModel):
    """TTS推理网关配置"""

    auth_token: str | None = None
    """节点接入鉴权Bearer token；None表示不校验，仅建议回环/可信内网使用"""

    request_timeout_s: float = 180.0
    """单次合成请求整体超时（秒），超时后向节点发送取消"""

    text_split_method: str = "cut5"
    """文本切分方法"""

    speed_factor: float = 1.0
    """语速，建议0.5-2.0"""

    seed: int = -1
    """采样随机种子，-1为每次随机，固定值可复现"""

    configs_file: str = "tts_configs.json"
    """运行时情绪映射配置文件名"""

    @property
    def configs_file_path(self) -> Path:
        return get_plugin_config_file(self.configs_file)


class Config(BaseModel):
    tts: ScopedConfig = ScopedConfig()


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).tts


class TTSEmotionPreset(AttrDocModel):
    """TTS情绪预设：情绪到参考音频的映射"""

    ref_audio_path: str
    """参考音频在推理节点上的本地路径（音色与情感来源）"""

    prompt_text: str
    """参考音频的文字转写"""

    prompt_lang: str = "ja"
    """参考音频语言（小写，如zh/ja/en）"""


class TTSConfigs(AttrDocModel):
    """TTS运行时配置文件"""

    tts_emotions: dict[str, TTSEmotionPreset] = {}
    """情绪到参考音频、转写与语言的映射；键限固定情绪枚举，缺项情绪回退默认"""

    @model_validator(mode="after")
    def _check_emotions(self) -> Self:
        """配置了情绪映射时：键限枚举值，且必须包含回退项"""
        if not self.tts_emotions:
            return self
        if unknown := set(self.tts_emotions) - set(TTS_EMOTIONS):
            raise ValueError(
                f"tts_emotions 含未知情绪键: {sorted(unknown)}，仅允许: {list(TTS_EMOTIONS)}"
            )
        if TTS_DEFAULT_EMOTION not in self.tts_emotions:
            raise ValueError(
                f"tts_emotions 必须包含默认情绪「{TTS_DEFAULT_EMOTION}」"
                f"（其余情绪缺项时回退到它）；完全不用TTS请将tts_emotions置空"
            )
        return self


generate_schema(TTSConfigs)
tts_configs = load_register_model_from_file(TTSConfigs, cfg.configs_file_path)
