from pydantic import BaseModel


class Config(BaseModel):
    chat_model: str
    chat_system_message_path: str
    chat_tavily_api_key: str
    chat_ban_path: str
    chat_bot_id: int
    chat_bot_nickname: str
