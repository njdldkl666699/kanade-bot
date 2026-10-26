from datetime import time
from pathlib import Path
from typing import Annotated, Literal

from nonebot import get_plugin_config, require
from pydantic import BaseModel, Field, NonNegativeInt, PositiveInt, WithJsonSchema

from kanade_bot.utils.billing import TokenBillingConfig
from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.schema import (
    AttrDocModel,
    BaseAgentConfig,
    ConfigRegistry,
    ProviderConfig,
    generate_schema,
)

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
    PromptSectionConfig(file="sandbox_python.md", when="python_env_available"),
    PromptSectionConfig(file="memes.md", when="memes_enabled"),
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

    sessions_dir: str = "sessions"
    """会话历史数据库目录，每个会话一个 SQLite 文件"""

    buffer_max_size: PositiveInt = 100
    """消息缓冲区最大条数，超出后丢弃最早的消息"""

    buffer_cache_file: str = "session_messages_cache.json"
    """消息缓冲区缓存文件名"""

    @property
    def sessions_dir_path(self) -> Path:
        return get_plugin_data_file(self.sessions_dir)

    @property
    def buffer_cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.buffer_cache_file)


REPLAY_RELEVANT_FIELDS = {"trigger_fraction", "keep_pairs", "context_window"}
"""决定压缩 mark 重放确定性的参数字段（进入 `CompactionConfig.fingerprint`）

- clear 档重放（`build_clear(online=False)`）只消费这三个字段；
- summarizing 档重放直接使用存储的 result，与参数无关；
- 其余字段只影响未来压缩的生成，不参与指纹。
"""


class CompactionConfig(AttrDocModel):
    """会话压缩配置"""

    trigger_fraction: float = 0.7
    """零成本档（清理工具结果）的触发比例

    摘要档启用时，轮末压缩由`summary_target_fraction`驱动，本值不参与；
    仅摘要档未启用时，轮末零成本档清理以本值为闸门。
    """

    keep_pairs: NonNegativeInt = 3
    """`ClearToolResults` 保留的最近工具调用对数"""

    min_clear_tokens: NonNegativeInt | None = None
    """清理收益低于此 token 数则跳过；**仅在线生效**
    
    `None` 表示不启用最小清理阈值。
    """

    context_window: int | None = None
    """上下文窗口覆盖值；None 时按模型 profile / genai-prices 解析"""

    summary_target_fraction: float | None = None
    """`TieredCompaction` 的停止预算（上下文占用比例）；None 表示不启用摘要档"""

    summary_model: str | None = None
    """摘要使用的模型 ID；`None` 表示继承主模型"""

    summary_keep_messages: NonNegativeInt = 40
    """生成摘要时保留的最近消息条数

    启用 `summary_keep_user_messages` 时，本值是「被摘要范围内保留的用户消息条数」与
    「原样保留的最近原始消息条数」共享的名额。用户消息优先占名额，剩余名额才留给最近的原始消息。
    """

    summary_keep_user_messages: bool = True
    """摘要压缩时保留被摘要范围内最近的用户消息
    
    每条有截断上限，由`SummarizingCompaction.keep_user_messages_max_chars`设置，
    默认20000字符，够用故暂不提供配置。
    """

    summary_prompt_file: str = "CompactionSummary.md"
    """摘要档提示词模板文件名

    模板须含`{messages}`占位符且无其它未转义花括号；文件缺失或非法时回退 harness 默认模板"""

    @property
    def summary_prompt_file_path(self) -> Path:
        """摘要档提示词模板文件路径"""
        return get_plugin_config_file(self.summary_prompt_file)

    online_valve_fraction: float | None = 0.92
    """run 内应急压缩阈值；None 表示完全关闭在线压缩

    常规压缩在轮次结束后进行；run 内仅在估算超过该阈值时才应急压缩，防止单轮内上下文暴涨击穿窗口。
    """

    def fingerprint(self) -> str:
        """重放相关参数指纹：仅覆盖决定压缩 mark 重放结果的字段"""
        return self.model_dump_json(include=REPLAY_RELEVANT_FIELDS)


class ScheduledTaskConfig(AttrDocModel):
    """Agent定时任务配置"""

    data_file: str = "scheduled_tasks.json"
    """定时任务持久化文件名"""

    retry_limit: NonNegativeInt = 2
    """触发失败后的重试次数（不含首次尝试）"""

    retry_delay_minutes: PositiveInt = 5
    """触发失败后的重试间隔（分钟）"""

    @property
    def data_file_path(self) -> Path:
        return get_plugin_data_file(self.data_file)


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
    """mirage沙箱配置"""

    enabled: bool = False
    """是否启用沙箱（文件与shell能力）

    需要 sandlock CLI 在 PATH 上"""

    environment: dict[str, str] = {}
    """沙箱环境变量，注入到每个会话

    注意：这些变量对工作区内的所有进程可见，不要在此存放敏感密钥"""

    memory_limit: str = "512M"
    """sandlock受限子进程的内存上限"""

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
    适配各部署环境不同的安装路径。仅当 sandlock 不在 PATH 时才需显式指定。"""

    uv_bin: str = "uv"
    """uv 可执行文件，PATH 上的名字或绝对路径

    不可用时自动回退到宿主 `python3 -m venv` 原生方式"""

    uv_python_dir: str | None = None
    """uv 托管解释器目录

    默认启动时执行 `uv python dir` 动态求值。"""

    venv_python: str = "3.13"
    """沙箱虚拟环境默认使用的 Python 版本

    uv 方式下可为 uv 托管解释器任意版本；原生回退方式下按 `python{版本}`
    在宿主查找（如 python3.13），找不到时用宿主默认 python3"""

    venv_timeout: int = 300
    """宿主侧执行 uv 命令的超时秒数"""

    @property
    def workspace_dir_path(self) -> Path:
        return get_plugin_data_file(self.workspace_dir)


class ImageCaptionConfig(BaseAgentConfig):
    """图片转述模型配置"""

    system_prompt_file: str = "ImageCaption.md"
    """系统提示词文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)


class StickerTaggerConfig(BaseAgentConfig):
    """表情包视觉打标模型配置"""

    system_prompt_file: str = "StickerTagging.md"
    """系统提示词文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)


class StickerEmbeddingConfig(AttrDocModel):
    """表情包 embedding 模型配置"""

    model: str = ""
    """embedding 模型 ID，留空视为未启用"""

    provider: ProviderConfig | None = None
    """embedding API 提供商配置，None 时使用 OpenAI 官方端点"""


class StickerMemeConfig(AttrDocModel):
    """表情包语义检索配置

    基于向量检索的表情包系统：图片由视觉模型自动打标，
    LLM 以 `{{表情: 描述}}` 占位符引用，投递时按语义相似度加权随机选图。
    """

    enabled: bool = False
    """是否启用表情包语义检索；未启用时占位符将被静默丢弃"""

    memes_dir: str = "memes/"
    """表情包存储目录名"""

    tagger: StickerTaggerConfig | None = None
    """视觉打标模型配置，None 时无法自动打标"""

    embedding: StickerEmbeddingConfig = StickerEmbeddingConfig()
    """embedding 模型配置"""

    topk: PositiveInt = 5
    """检索候选数：TopK 内按相似度 softmax 加权随机抽取"""

    min_similarity: float = 0.3
    """最低余弦相似度，低于该值的候选不参与抽取"""

    softmax_scale: float = 55.0
    """相似度 softmax 温度系数，越大越偏向最高分"""

    index_file: str = "memes_index.json"
    """索引元数据文件名（json）"""

    matrix_file: str = "memes_index.npy"
    """向量矩阵文件名（npy）"""

    @property
    def memes_dir_path(self) -> Path:
        """表情包存储目录路径"""
        return get_plugin_data_file(self.memes_dir)

    @property
    def index_file_path(self) -> Path:
        """索引元数据文件路径"""
        return get_plugin_data_file(self.index_file)

    @property
    def matrix_file_path(self) -> Path:
        """向量矩阵文件路径"""
        return get_plugin_data_file(self.matrix_file)


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


class ReplyGateConfig(BaseAgentConfig):
    """主动回复决策节点模型配置"""

    system_prompt_file: str = "ReplyGate.md"
    """插话决策系统提示词文件名，缺失或为空时回退内置默认"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)


_TIME_OF_DAY_SCHEMA: dict[str, str] = {"type": "string", "pattern": r"^\d{1,2}:\d{2}(:\d{1,2})?$"}
"""时间字段的自定义 JSON Schema：HH:MM（可选秒），不按 RFC3339 校验"""


class TimeWindow(AttrDocModel):
    """主动回复时段概率窗口"""

    start: Annotated[time, WithJsonSchema(_TIME_OF_DAY_SCHEMA)]
    """起始时间 HH:MM（含）"""

    end: Annotated[time, WithJsonSchema(_TIME_OF_DAY_SCHEMA)]
    """结束时间 HH:MM（不含），支持跨零点（如 23:00 → 07:00）"""

    weekdays_only: bool = False
    """仅工作日生效（周一至周五，剔除法定节假日）"""

    probability: float = Field(default=1.0, ge=0.0, le=1.0)
    """该时段每批送决策的概率；0 = 关闭主动回复，1 = 每批必送"""


class AutoReplyConfig(AttrDocModel):
    """主动回复配置

    仅群聊生效

    是否回复由 LLM 决策节点结合群聊内容判定；

    上下文压缩复用 `compaction` 摘要档配置。
    """

    enabled: bool = False
    """总开关"""

    gate: ReplyGateConfig = ReplyGateConfig()
    """决策节点模型配置，model留空回退主模型"""

    time_windows: list[TimeWindow] = []
    """时段概率表：默认为空（任何时段每批必送）；多个窗口命中时取最先声明者，都不命中时概率为 1.0"""

    backoff_start: int = 2
    """连续决策不回 N 次后开始退避"""

    backoff_base: float = 60.0
    """退避基准秒：延迟 = base × 2^(count - start)"""

    backoff_cap: float = 1800.0
    """退避上限秒"""

    gate_timeout: float = 30.0
    """决策调用超时（秒），超时按不回复处理且不计退避"""


class ScopedConfig(AttrDocModel):
    agent: AgentConfig = AgentConfig()
    """聊天Agent配置"""

    session: SessionConfig = SessionConfig()
    """会话历史存储配置"""
    compaction: CompactionConfig = CompactionConfig()
    """会话压缩配置"""
    memory: MemoryConfig = MemoryConfig()
    """持久化记忆配置"""

    scheduled_task: ScheduledTaskConfig = ScheduledTaskConfig()
    """Agent定时任务配置"""
    sandbox: SandboxConfig = SandboxConfig()
    """mirage沙箱配置"""

    sticker_meme: StickerMemeConfig = StickerMemeConfig()
    """表情包语义检索配置"""
    image_caption: ImageCaptionConfig | None = None
    """图片转述模型配置，如果为None则不启用图片转述。

    不启用且主模型不支持图片输入，则无法处理图片消息。
    """

    rag: RAGConfig = RAGConfig()
    tts: TTSConfig = TTSConfig()

    auto_reply: AutoReplyConfig = AutoReplyConfig()
    """主动回复配置"""

    billing: TokenBillingConfig = TokenBillingConfig()
    """按Token计费的费率配置"""

    configs_file: str = "chat_configs.json"
    """聊天配置文件名"""
    fail_image_file: str = "chat_fail.jpg"
    """聊天失败时发送的图片名，不存在则返回默认的文本消息"""

    @property
    def configs_file_path(self) -> Path:
        return get_plugin_config_file(self.configs_file)

    @property
    def fail_image_file_path(self) -> Path:
        return get_plugin_config_file(self.fail_image_file)


class Config(BaseModel):
    chat: ScopedConfig = ScopedConfig()


ConfigRegistry.register_config_types(Config)

cfg = get_plugin_config(Config).chat


class ChatConfig(AttrDocModel):
    """聊天配置"""

    banned_users: set[str] = set()
    """拉黑的用户ID列表"""
    banned_groups: set[str] = set()
    """拉黑的群ID列表"""
    auto_reply_enabled_groups: set[str] = set()
    """启用主动回复的群ID列表"""


class ChatConfigs(AttrDocModel):
    """聊天配置文件"""

    console: ChatConfig = ChatConfig()
    onebot: ChatConfig = ChatConfig()

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


generate_schema(ChatConfigs)
chat_configs = load_register_model_from_file(ChatConfigs, cfg.configs_file_path)
