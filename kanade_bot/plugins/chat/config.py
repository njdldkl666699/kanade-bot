from pathlib import Path
from typing import Literal

from nonebot import get_plugin_config, require
from pydantic import BaseModel, PositiveInt

from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.schema import AttrDocModel, BaseAgentConfig, ConfigRegistry, generate_schema

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import (
    get_plugin_cache_file,
    get_plugin_config_file,
    get_plugin_data_file,
)

require("model_updater")
from kanade_bot.plugins.model_updater import load_register_model_from_file

DEFAULT_PROMPT_FILES = [
    "identity.md",
    "chat_style.md",
    "lore_qa.md",
    "tool_usage.md",
    "tools.md",
    "system_notifications.md",
    "wiki.md",
    "abbreviations.md",
    "environment.md",
]


class PromptSectionConfig(AttrDocModel):
    """会话层提示词片段"""

    file: str
    """提示词文件名，相对提示词目录"""

    when: str = ""
    """条件变量名
    
    该变量为真值时才拼接本段，否则整段跳过。空串表示无条件拼接。"""


DEFAULT_PROMPT_SECTIONS = [
    PromptSectionConfig(file="sandbox.md", when="sandbox_enabled"),
    PromptSectionConfig(file="group_chat.md", when="is_group"),
]


class ChatPromptConfig(AttrDocModel):
    """聊天Agent的模块化系统提示词配置"""

    dir: str = "prompts/"
    """提示词目录，相对插件配置目录"""

    files: list[str] = DEFAULT_PROMPT_FILES
    """静态层片段文件名（相对 `dir`），按顺序拼接

    只允许出现**进程恒定**的内容，构造时一次性求值。
    """

    sections: list[PromptSectionConfig] = DEFAULT_PROMPT_SECTIONS
    """会话层片段，在 `files` 之后按配置顺序拼接，每轮重新求值

    只能放**每会话恒定**的内容。"""

    fallback: str = "你是一只可爱的猫娘。"
    """静态层片段全部缺失时的兜底提示词"""

    vars: dict[str, str] = {}
    """自定义模板变量，可覆盖内置变量；优先级最高

    适合放不会变的注入常量。"""

    @property
    def dir_path(self) -> Path:
        """提示词目录路径"""
        return get_plugin_config_file(self.dir)


class AgentConfig(BaseAgentConfig):
    """聊天Agent配置"""

    prompt: ChatPromptConfig = ChatPromptConfig()
    """模块化系统提示词配置"""


class SessionConfig(AttrDocModel):
    """会话历史存储配置"""

    db_file: str = "agent_sessions.sqlite3"
    """会话历史 SQLite 数据库文件名"""

    buffer_max_size: PositiveInt = 100
    """消息缓冲区最大条数，超出后丢弃最早的消息"""

    buffer_cache_file: str = "session_messages_cache.json"
    """消息缓冲区缓存文件名"""

    @property
    def db_file_path(self) -> Path:
        return get_plugin_data_file(self.db_file)

    @property
    def buffer_cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.buffer_cache_file)


class CompactionConfig(AttrDocModel):
    """会话压缩配置"""

    trigger_fraction: float = 0.8
    """触发清理的上下文占用比例"""

    keep_pairs: PositiveInt = 3
    """`ClearToolResults` 保留的最近工具调用对数"""

    min_clear_tokens: PositiveInt = 2_000
    """清理收益低于此 token 数则跳过；**仅在线生效**"""

    context_window: int | None = None
    """上下文窗口覆盖值；None 时按模型 profile / genai-prices 解析"""

    summary_target_fraction: float | None = None
    """`TieredCompaction` 的停止预算（上下文占用比例）；None 表示不启用摘要档"""

    summary_model: str | None = None
    """摘要使用的模型 ID；None 表示继承主模型"""

    summary_keep_messages: PositiveInt = 40
    """生成摘要时保留的最近消息条数"""

    def fingerprint(self) -> str:
        """参数指纹：用于检测配置漂移"""
        return self.model_dump_json()


class MemoryConfig(AttrDocModel):
    """持久化记忆配置"""

    database_file: str = "memories.sqlite3"
    """持久化记忆数据库文件名，位于插件数据目录下"""

    max_records_per_scope: PositiveInt = 256
    """每个用户或群聊最多保留的记忆条数，超出后淘汰最久未更新的记录"""

    @property
    def database_file_path(self) -> Path:
        return get_plugin_data_file(self.database_file)


class SandboxConfig(AttrDocModel):
    """Mirage沙箱配置"""

    enabled: bool = False
    """是否启用沙箱（文件与shell能力）

    需要 sandlock CLI 在 PATH 上"""

    environment: dict[str, str] = {}
    """沙箱环境变量，注入到每个会话

    注意：这些变量对工作区内的所有进程可见，不要在此存放敏感密钥"""

    memory_limit: str = "512M"
    """sandlock受限子进程的内存上限"""

    max_concurrent_sandboxes: PositiveInt = 4
    """同时存活的最大沙箱数，超出后LRU关闭"""

    idle_timeout_minutes: PositiveInt = 30
    """空闲沙箱回收阈值，超时后关闭"""

    sweeper_interval_minutes: PositiveInt = 5
    """后台回收任务扫描间隔"""

    workspace_dir: str = "sandboxes/"
    """沙箱工作区根目录名，位于插件缓存目录；每个聊天会话一个子目录"""

    landlock_degrade: Literal["auto", "always", "strict"] = "auto"
    """Landlock ABI 不足时的策略

    - `auto`（默认）：按 `sandlock check` 的结果自动决定，
      ABI < v6 时启用wrapper 注入 `--allow-degraded`
    - `always`：始终注入降级参数
    - `strict`：ABI 不足 v6 即报错"""

    landlock_real_binary: str | None = None
    """真实 sandlock 可执行文件的绝对路径

    默认：生成 wrapper 时用 `shutil.which("sandlock")` 动态求值，
    适配各部署环境不同的安装路径。仅当 sandlock 不在 PATH时才需显式指定。"""

    @property
    def workspace_dir_path(self) -> Path:
        return get_plugin_cache_file(self.workspace_dir)


class ImageCaptionConfig(BaseAgentConfig):
    """图片转述模型配置"""

    system_prompt_file: str = "ImageCaption.md"
    """系统提示词文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件的路径"""
        return get_plugin_config_file(self.system_prompt_file)


class RAGConfig(AttrDocModel):
    """RAG相关配置"""

    enabled: bool = False
    """是否启用RAG功能"""
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


class TTSConfig(AttrDocModel):
    """文本转语音模型配置"""

    base_url: str | None = None
    """TTS服务的Base URL，如果为None则不启用TTS"""
    model: str | None = None
    """TTS使用的模型名称，不配置则使用服务端默认模型"""
    voice: str | None = None
    """TTS使用的声音类型，不配置则使用服务端默认模型"""


class ScopedConfig(AttrDocModel):
    agent: AgentConfig = AgentConfig()
    """聊天Agent配置"""

    session: SessionConfig = SessionConfig()
    """会话历史存储配置"""
    compaction: CompactionConfig = CompactionConfig()
    """会话压缩配置"""
    memory: MemoryConfig = MemoryConfig()
    """持久化记忆配置"""

    sandbox: SandboxConfig = SandboxConfig()
    """Mirage沙箱配置"""
    image_caption: ImageCaptionConfig | None = None
    """图片转述模型配置，如果为None则不启用图片转述。

    不启用且主模型不支持图片输入，则无法处理图片消息。
    """
    rag: RAGConfig = RAGConfig()
    tts: TTSConfig = TTSConfig()

    configs_file: str = "chat_configs.json"
    """聊天配置文件名"""
    fail_image_file: str = "chat_fail.jpg"
    """聊天失败时发送的图片名，不存在则返回默认的文本消息"""
    memes_dir: str = "memes/"
    """表情包存储目录名"""

    @property
    def configs_file_path(self) -> Path:
        return get_plugin_config_file(self.configs_file)

    @property
    def fail_image_file_path(self) -> Path:
        return get_plugin_config_file(self.fail_image_file)

    @property
    def memes_dir_path(self) -> Path:
        return get_plugin_data_file(self.memes_dir)


class Config(BaseModel):
    chat: ScopedConfig = ScopedConfig()


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).chat


class AutoReplyConfig(AttrDocModel):
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


class ChatConfig(AttrDocModel):
    """聊天配置"""

    banned_users: set[str] = set()
    """拉黑的用户ID列表"""
    banned_groups: set[str] = set()
    """拉黑的群ID列表"""
    auto_reply_group_config: dict[str, AutoReplyConfig] = {}
    """主动回复配置，键为群ID，值为AutoReplyConfig对象"""


class ChatConfigs(AttrDocModel):
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


generate_schema(ChatConfigs)
chat_configs = load_register_model_from_file(ChatConfigs, cfg.configs_file_path)
