from pathlib import Path
from typing import Literal

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    chat_model: str = "gpt-4.1"
    """模型ID，需要支持图片输入"""
    chat_system_prompt_path: str = ""
    """系统提示词文件路径"""
    chat_tavily_api_key: str
    """Tavily API Key"""
    chat_configs_path: str = "chat_configs.json"
    """聊天配置文件路径"""
    chat_bot_id: int
    """使用OneBot协议时，聊天机器人的QQ号"""
    chat_bot_nickname: str
    """使用OneBot协议时，聊天机器人的昵称"""


class AutoReplyConfig(BaseModel):
    """主动回复配置

    该配置用于设置在群聊中达到一定消息量后自动回复的行为，包括触发阈值和回复概率

    达到触发阈值后，机器人会触发一次抽取自动回复的行为，根据设定的概率决定是否进行回复，
    如果抽中，则清空上下文并回复一条消息，未抽中，则下次收到消息再次抽取。

    主动回复消息不受用户黑名单限制，但是受群聊黑名单限制
    """

    threshold: int = 0
    """自动回复的消息阈值，单位为条，小于等于0时不触发"""
    probability: float = 1.0
    """达到阈值后自动回复的概率，取值范围为0.0到1.0"""


class ChatConfig(BaseModel):
    """聊天配置"""

    banned_users: set[str] = set()
    """拉黑的用户ID列表"""
    banned_groups: set[str] = set()
    """拉黑的群ID列表"""
    auto_reply_group_config: dict[str, AutoReplyConfig] = {}
    """主动回复配置，键为群ID，值为AutoReplyConfig对象"""


type PlatformType = Literal["console", "onebot"]


class ChatConfigs(BaseModel):
    """聊天配置文件，包含Console和OneBot两部分"""

    console: ChatConfig = ChatConfig()
    onebot: ChatConfig = ChatConfig()


cfg = get_plugin_config(Config)


def _ensure_get_chat_config() -> ChatConfigs:
    """确保聊天配置文件存在并返回配置对象，如果文件不存在则创建默认配置文件并返回默认配置对象"""
    path = Path(cfg.chat_configs_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        default_configs = ChatConfigs()
        path.write_text(
            default_configs.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return default_configs

    return ChatConfigs.model_validate_json(path.read_text(encoding="utf-8"))


configs = _ensure_get_chat_config()


def write_chat_config():
    """将聊天配置对象写入配置文件"""
    Path(cfg.chat_configs_path).write_text(
        configs.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )
