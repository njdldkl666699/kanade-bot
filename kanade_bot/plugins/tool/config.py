from nonebot.adapters.onebot.v11 import Message
from pydantic import BaseModel, RootModel


class ScopedConfig(BaseModel):
    fallback_icon_file_path: str = "assets/images/grass_block.png"
    """服务器图标加载失败时的替代图标路径，支持PNG格式。"""
    template_file_path: str = "assets/mcstatus_template.html"
    """服务器状态渲染使用的HTML模板路径，支持HTML格式。"""
    schedule_configs_file_path: str = "assets/schedule_configs.json"
    """定时任务配置文件路径。暂仅支持OneBot v11群聊。"""


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
