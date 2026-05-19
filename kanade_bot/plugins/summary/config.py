from pydantic import BaseModel


class ScopedConfig(BaseModel):
    model: str = "gpt-5-mini"
    """总结使用的模型ID"""
    system_prompt_file_path: str = "assets/prompts/Summarizer.md"
    """总结使用的系统提示词文件路径"""
    bot_name: str = "宵崎奏"
    """总结中AI的名称，用于区分用户和AI的发言"""
    min_size: int = 10
    """总结的最小消息条数"""
    max_size: int = 2048
    """总结的最大条数，同时也是消息记录的最大条数，超过后会丢弃最早的消息"""
    message_records_file_path: str = "assets/summary_message_records.json"
    """总结消息记录的缓存文件路径"""


class Config(BaseModel):
    summary: ScopedConfig
