from pathlib import Path

from nonebot import require
from nonebot.adapters.onebot.v11 import Message
from pydantic import BaseModel, RootModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_file


class ScopedConfig(BaseModel):
    fallback_icon_file: str = "grass_block.png"
    """服务器图标加载失败时的替代图标名，支持PNG格式。"""
    template_file: str = "mcstatus_template.html"
    """服务器状态渲染使用的HTML模板文件名，支持HTML格式。"""
    schedule_configs_file: str = "schedule_configs.json"
    """定时任务配置文件名。暂仅支持OneBot v11群聊。"""

    @property
    def fallback_icon_file_path(self) -> Path:
        """服务器图标加载失败时的替代图标路径"""
        return get_plugin_config_file(self.fallback_icon_file)

    @property
    def template_file_path(self) -> Path:
        """服务器状态渲染使用的HTML模板文件路径"""
        return get_plugin_config_file(self.template_file)

    @property
    def schedule_configs_file_path(self) -> Path:
        """定时任务配置文件路径"""
        return get_plugin_config_file(self.schedule_configs_file)


class Config(BaseModel):
    tool: ScopedConfig


class ScheduleConfig(BaseModel):
    """定时任务配置"""

    cron: str
    """定时任务的Cron表达式"""
    message: Message
    """定时任务发送的消息内容"""


class ScheduleConfigs(RootModel[dict[int, dict[str, ScheduleConfig]]]):
    """定时任务配置列表

    键为群号，值为该群的定时任务列表：
        键为定时任务名称，每个群内不可重复
        值为定时任务配置
    """
