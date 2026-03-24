from pydantic import BaseModel


class Config(BaseModel):
    chat_model: str = "gpt-4.1"
    """模型ID，需要支持图片输入"""
    chat_system_prompt_path: str = ""
    """系统提示词文件路径"""
    chat_prompt_buffer_size: int = -1
    """会话消息缓冲区大小，大于等于该大小后将主动发送消息，小于等于0表示不限制"""
    chat_tavily_api_key: str
    """Tavily API Key"""
    chat_ban_path: str = "chat_ban.json"
    """聊天黑名单文件路径"""
    chat_bot_id: int
    """使用OneBot协议时，聊天机器人的QQ号"""
    chat_bot_nickname: str
    """使用OneBot协议时，聊天机器人的昵称"""
