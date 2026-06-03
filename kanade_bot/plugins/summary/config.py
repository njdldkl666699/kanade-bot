from pathlib import Path

from copilot import ProviderConfig
from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_cache_file, get_plugin_config_file


class ScopedConfig(BaseModel):
    model: str = "gpt-5-mini"
    """模型ID"""
    provider: ProviderConfig | None = None
    """模型提供商配置，如果为None则使用Copilot内置模型"""
    system_prompt_file: str = "Summarizer.md"
    """系统提示词文件名"""
    bot_name: str = "宵崎奏"
    """总结中AI的名称，用于区分用户和AI的发言"""
    min_size: int = 10
    """最小消息条数"""
    max_size: int = 2048
    """最大条数，同时也是消息记录的最大条数，超过后会丢弃最早的消息"""
    message_records_file: str = "summary_message_records.json"
    """消息记录的缓存文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)

    @property
    def message_records_file_path(self) -> Path:
        """消息记录的缓存文件路径"""
        return get_plugin_cache_file(self.message_records_file)


class Config(BaseModel):
    summary: ScopedConfig = ScopedConfig()
