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


class ProactiveConfig(AttrDocModel):
    """主动回复配置

    仅群聊生效，是否回复由 LLM 决策节点结合群聊内容判定；

    决策上下文每日定时清空，规模受当日边界约束；超活跃群单日超限时护栏
    截断保留最近 N 个判定轮，不做摘要压缩。
    """

    enabled: bool = False
    """总开关"""

    gate: ReplyGateConfig = ReplyGateConfig()
    """决策节点模型配置，model留空回退主模型"""

    time_windows: list[TimeWindow] = []
    """时段概率表：默认为空（任何时段每批必送）；多个窗口命中时取最先声明者，都不命中时概率为 1.0"""

    daily_reset_time: Annotated[time, WithJsonSchema(_TIME_OF_DAY_SCHEMA)] = time(4, 0)
    """每日决策上下文清空时刻（Asia/Shanghai，进程启动时读取）：到点清空内存生效序列，DB 全量保留，重启仅恢复当日"""

    context_usage_threshold: float = 0.7
    """决策上下文护栏：估算 tokens 超决策模型上下文窗口此比例时截断内存生效序列，保留最近 N 个判定轮（0 = 关闭护栏）"""

    context_keep_decisions: int = 6
    """护栏触发时保留的最近判定轮数"""

    backoff_start: int = 2
    """连续决策不回 N 次后开始退避"""

    backoff_base: float = 60.0
    """退避基准秒：延迟 = base × 2^(count - start)"""

    backoff_cap: float = 1800.0
    """退避上限秒"""

    gate_timeout: float = 30.0
    """决策调用超时（秒），超时按不回复处理且不计退避"""

    buffer_gate_threshold: int = 16
    """待判定消息量阈值：自上次判定以来的新增群消息达到该条数的整数倍时额外尝试一次决策（0 = 关闭）"""
