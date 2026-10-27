"""主动回复配置"""

from datetime import time
from pathlib import Path
from typing import Annotated

from nonebot import require
from pydantic import Field, WithJsonSchema

from kanade_bot.utils.schema import AttrDocModel, BaseAgentConfig

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file


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
