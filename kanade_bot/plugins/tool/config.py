from pathlib import Path

from nonebot import get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Message
from pydantic import BaseModel, RootModel


class Config(BaseModel):
    tool_fallback_icon_file_path: str = "assets/images/grass_block.png"
    """服务器图标加载失败时的替代图标路径，支持PNG格式。"""
    tool_template_file_path: str = "assets/mcstatus_template.html"
    """服务器状态渲染使用的HTML模板路径，支持HTML格式。"""
    tool_schedule_configs_file_path: str = "assets/schedule_configs.json"
    """定时任务配置文件路径。暂仅支持OneBot v11群聊。"""


cfg = get_plugin_config(Config)


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


def _ensure_schedules() -> ScheduleConfigs:
    path = Path(cfg.tool_schedule_configs_file_path)
    if not path.exists():
        logger.warning(f"定时任务配置文件 {path} 不存在，已创建空文件")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
        return ScheduleConfigs({})

    try:
        return ScheduleConfigs.model_validate_json(path.read_text())
    except Exception as e:
        logger.error(f"加载定时任务配置失败: {e}")
        return ScheduleConfigs({})


schedules: ScheduleConfigs = _ensure_schedules()


def write_schedules():
    """将定时任务配置写入配置文件"""
    Path(cfg.tool_schedule_configs_file_path).write_text(
        schedules.model_dump_json(indent=2, ensure_ascii=False)
    )
