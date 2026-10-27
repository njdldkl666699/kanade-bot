"""聊天插件配置：按域拆分的配置模型汇总层"""

from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel

from kanade_bot.utils.billing import TokenBillingConfig
from kanade_bot.utils.schema import ConfigRegistry

from .agent import AgentConfig, ChatPromptConfig, ImageCaptionConfig, PromptSectionConfig, TTSConfig
from .auto_reply import AutoReplyConfig, ReplyGateConfig, TimeWindow
from .history import CompactionConfig, SessionConfig
from .memes import StickerEmbeddingConfig, StickerMemeConfig, StickerTaggerConfig
from .memory import MemoryConfig
from .rag import RAGConfig
from .runtime import ChatConfig, ChatConfigs
from .sandbox import SandboxConfig
from .schedule import ScheduledTaskConfig

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file

require("model_updater")
from kanade_bot.plugins.model_updater import load_register_model_from_file

__all__ = [
    "AgentConfig",
    "AutoReplyConfig",
    "ChatConfig",
    "ChatConfigs",
    "ChatPromptConfig",
    "CompactionConfig",
    "Config",
    "ImageCaptionConfig",
    "MemoryConfig",
    "PromptSectionConfig",
    "RAGConfig",
    "ReplyGateConfig",
    "SandboxConfig",
    "ScheduledTaskConfig",
    "ScopedConfig",
    "SessionConfig",
    "StickerEmbeddingConfig",
    "StickerMemeConfig",
    "StickerTaggerConfig",
    "TTSConfig",
    "TimeWindow",
    "cfg",
    "chat_configs",
]


class ScopedConfig(BaseModel):
    agent: AgentConfig = AgentConfig()
    """聊天Agent配置"""

    session: SessionConfig = SessionConfig()
    """会话历史存储配置"""
    compaction: CompactionConfig = CompactionConfig()
    """会话压缩配置"""
    memory: MemoryConfig = MemoryConfig()
    """持久化记忆配置"""

    scheduled_task: ScheduledTaskConfig = ScheduledTaskConfig()
    """Agent定时任务配置"""
    sandbox: SandboxConfig = SandboxConfig()
    """mirage沙箱配置"""

    sticker_meme: StickerMemeConfig = StickerMemeConfig()
    """表情包语义检索配置"""
    image_caption: ImageCaptionConfig | None = None
    """图片转述模型配置，如果为None则不启用图片转述。

    不启用且主模型不支持图片输入，则无法处理图片消息。
    """

    rag: RAGConfig = RAGConfig()
    tts: TTSConfig = TTSConfig()

    auto_reply: AutoReplyConfig = AutoReplyConfig()
    """主动回复配置"""

    billing: TokenBillingConfig = TokenBillingConfig()
    """按Token计费的费率配置"""

    configs_file: str = "chat_configs.json"
    """聊天配置文件名"""
    fail_image_file: str = "chat_fail.jpg"
    """聊天失败时发送的图片名，不存在则返回默认的文本消息"""

    @property
    def configs_file_path(self) -> Path:
        return get_plugin_config_file(self.configs_file)

    @property
    def fail_image_file_path(self) -> Path:
        return get_plugin_config_file(self.fail_image_file)


class Config(BaseModel):
    chat: ScopedConfig = ScopedConfig()


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).chat

chat_configs = load_register_model_from_file(ChatConfigs, cfg.configs_file_path)
