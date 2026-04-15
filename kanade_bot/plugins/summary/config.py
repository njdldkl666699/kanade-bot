from pydantic import BaseModel


class Config(BaseModel):
    summary_enabled: bool = True
    """是否启用总结功能"""
    summary_model: str = "gpt-5-mini"
    """总结使用的模型ID"""
    summary_system_prompt_path: str
    """总结使用的系统提示词文件路径"""
    summary_bot_name: str = "宵崎奏"
    """总结中AI的名称，用于区分用户和AI的发言"""
    summary_min_size: int = 10
    """总结的最小消息条数"""
    summary_max_size: int = 2048
    """总结的最大条数，同时也是消息记录的最大条数，超过后会丢弃最早的消息"""
    summary_message_records_path: str = "summary_message_records.json"
    """总结消息记录的缓存文件路径"""
