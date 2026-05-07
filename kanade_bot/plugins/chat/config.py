from pathlib import Path

from nonebot import get_plugin_config
from pydantic import BaseModel


class Config(BaseModel):
    chat_model: str = "gpt-5-mini"
    """模型ID，需要支持图片输入"""
    chat_system_prompt_file_path: str = "assets/prompts/Kanade-v3.md"
    """系统提示词文件路径"""
    chat_tavily_api_key: str
    """Tavily API Key"""

    chat_session_prompt_buffer_max_size: int = 100
    """会话消息缓冲区最大条数，超出后会丢弃最早的消息"""
    chat_configs_file_path: str = "assets/chat_configs.json"
    """聊天配置文件路径"""
    chat_memes_dir_path: str = "assets/memes"
    """表情包存储路径"""
    chat_fail_image_file_path: str = "assets/images/chat_fail.jpg"
    """聊天失败时发送的图片路径，不存在则返回默认的文本消息"""
    chat_memories_dir_path: str = "assets/memories"
    """记忆文件存储路径，每个记忆为一个 Markdown 文件"""

    chat_rag_enabled: bool = False
    """是否启用RAG功能"""
    chat_rag_port: int = 39831
    """RAG服务器端口号"""
    chat_rag_query_n_results: int = 3
    """RAG查询返回的相关文档数量"""
    chat_rag_distance_threshold: float = 0.7
    """RAG相关文档的距离阈值，数值越小表示越相关"""


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
    path = Path(cfg.chat_configs_file_path)
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
    Path(cfg.chat_configs_file_path).write_text(
        configs.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )
