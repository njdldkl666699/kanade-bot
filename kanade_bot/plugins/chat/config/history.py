"""会话历史存储与压缩配置"""

from pathlib import Path

from nonebot import require
from pydantic import NonNegativeInt, PositiveInt

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import (
    get_plugin_cache_file,
    get_plugin_config_file,
    get_plugin_data_file,
)


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
