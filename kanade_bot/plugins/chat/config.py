from pathlib import Path
from typing import Literal

from copilot import ProviderConfig
from nonebot import get_plugin_config, require
from pydantic import BaseModel

from kanade_bot.utils.common import PlatformType

require("nonebot_plugin_localstore")
require("model_updater")

from nonebot_plugin_localstore import get_plugin_config_file, get_plugin_data_file

from kanade_bot.plugins.model_updater import load_register_model_from_file


class RAGConfig(BaseModel):
    """RAG相关配置"""

    query_n_results: int = 3
    """查询返回的相关文档数量"""
    distance_threshold: float = 0.65
    """相关文档的距离阈值，数值越小表示越相关"""

    db_dir: str = "rag_db/"
    """向量数据库的存储目录名"""
    collection_name: str = "kanade_wiki_collection"
    """向量数据库中集合的名称"""
    document_file: str = "kanade_wiki.json"
    """初始文档文件名，位于插件配置目录下，格式为JSON，每个文档包含id、text和metadata字段"""

    embedding_type: Literal["sentence_transformer", "openai"] = "sentence_transformer"
    """RAG使用的向量化方法
    - `sentence_transformer`需要指定model_name_or_path参数
    - `openai`需要指定openai开头的参数
    """
    model_name_or_path: str = "BAAI/bge-small-zh-v1.5"
    """RAG使用的模型名称或路径，支持从Hugging Face下载"""
    openai_api_key: str | None = None
    """OpenAI API密钥"""
    openai_base_url: str | None = None
    """OpenAI API Base URL，可选，用于自定义端点"""
    openai_model: str = "text-embedding-3-small"
    """OpenAI嵌入模型名称"""

    @property
    def db_dir_path(self) -> Path:
        """向量数据库的存储目录路径"""
        return get_plugin_data_file(self.db_dir)

    @property
    def document_file_path(self) -> Path:
        """初始文档文件的路径"""
        return get_plugin_config_file(self.document_file)


class ScopedConfig(BaseModel):
    model: str = "gpt-5-mini"
    """模型ID，需要支持图片输入"""
    provider: ProviderConfig | None = None
    """模型提供商配置，如果为None则使用Copilot内置模型"""
    system_prompt_file: str = "Kanade-v3.md"
    """系统提示词文件名"""
    tavily_api_key: str
    """Tavily API Key"""
    session_prompt_buffer_max_size: int = 100
    """会话消息缓冲区最大条数，超出后会丢弃最早的消息"""

    configs_file: str = "chat_configs.json"
    """聊天配置文件名"""
    fail_image_file: str = "chat_fail.jpg"
    """聊天失败时发送的图片名，不存在则返回默认的文本消息"""

    memes_dir: str = "memes/"
    """表情包存储目录名"""
    memories_dir: str = "memories/"
    """记忆文件存储目录名称，每个记忆为一个 Markdown 文件"""

    rag: RAGConfig = RAGConfig()

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件的路径"""
        return get_plugin_config_file(self.system_prompt_file)

    @property
    def configs_file_path(self) -> Path:
        """聊天配置文件的路径"""
        return get_plugin_config_file(self.configs_file)

    @property
    def fail_image_file_path(self) -> Path:
        """聊天失败时发送的图片的路径"""
        return get_plugin_config_file(self.fail_image_file)

    @property
    def memes_dir_path(self) -> Path:
        """表情包存储目录的路径"""
        return get_plugin_data_file(self.memes_dir)

    @property
    def memories_dir_path(self) -> Path:
        """记忆文件存储目录的路径"""
        return get_plugin_data_file(self.memories_dir)


class Config(BaseModel):
    chat: ScopedConfig


cfg = get_plugin_config(Config).chat


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

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


chat_configs = load_register_model_from_file(ChatConfigs, cfg.configs_file_path)
