"""Agent 定时任务配置"""

from pathlib import Path

from nonebot import require
from pydantic import NonNegativeInt, PositiveInt

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_data_file


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
