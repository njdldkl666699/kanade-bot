from pathlib import Path
from typing import Literal

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    chat_model: str = "gpt-4.1"
    """模型ID，需要支持图片输入"""
    chat_system_prompt_path: str
    """系统提示词文件路径"""
    chat_tavily_api_key: str
    """Tavily API Key"""

    chat_session_prompt_buffer_max_size: int = 100
    """会话消息缓冲区最大条数，超出后会丢弃最早的消息"""
    chat_configs_path: str = "chat_configs.json"
    """聊天配置文件路径"""
    chat_memes_path: str = "memes"
    """表情包存储路径"""

    chat_bot_id: int
    """使用OneBot协议时，聊天机器人的QQ号"""
    chat_bot_nickname: str
    """使用OneBot协议时，聊天机器人的昵称"""

    chat_rag_enabled: bool = False
    """是否启用RAG功能"""
    chat_rag_port: int = 8000
    """RAG使用的ChromaDB服务端口号"""
    chat_rag_model_or_path: str = "BAAI/bge-small-zh-v1.5"
    """RAG使用的模型名称或路径，默认为BGE小型中文模型，支持从Hugging Face下载"""
    chat_rag_db_collection_name: str = "kanade_wiki_collection"
    """RAG使用的ChromaDB集合名称"""
    chat_rag_query_n_results: int = 3
    """RAG查询返回的相关文档数量"""
    chat_rag_score_threshold: float = 0.7
    """RAG相关文档的相似度分数阈值，数值越小表示越相关"""


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
    """聊天配置文件"""

    console: ChatConfig = ChatConfig()
    onebot: ChatConfig = ChatConfig()

    memes: dict[str, str | None] = {}
    """表情包列表

    每个表情包为一个字典，key为表情包名称，value为表情包描述。

    每个表情包在`CHAT_MEMES_PATH`目录下有一个同名子目录，存放该表情包的图片。
    """


cfg = get_plugin_config(Config)


def _ensure_chat_configs() -> ChatConfigs:
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


configs = _ensure_chat_configs()


def write_chat_config():
    """将聊天配置对象写入配置文件"""
    Path(cfg.chat_configs_path).write_text(
        configs.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )
