from datetime import date
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, require
from pydantic import BaseModel, RootModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_data_file


class Config(BaseModel):
    command_counter_data_file: str = "command_counter_data.json"
    """命令计数器保存的数据文件名"""

    @property
    def command_counter_data_file_path(self) -> Path:
        return get_plugin_data_file(self.command_counter_data_file)


cfg = get_plugin_config(Config)


class CommandCounterData(RootModel[dict[date, dict[str, int]]]):
    """命令计数器数据模型，保存每一天的命令计数

    按日期保存命令计数，每一天的命令计数是一个字典，键为命令名，值为计数。
    """

    @classmethod
    def load(cls):
        """从文件加载命令计数器数据"""
        fp = cfg.command_counter_data_file_path
        if not fp.exists():
            logger.warning(f"命令计数器数据文件 {fp} 不存在，使用默认值")
            return cls({})
        return cls.model_validate_json(fp.read_text(encoding="utf-8"))


command_counter_data = CommandCounterData.load()


driver = get_driver()


@driver.on_shutdown
def save_command_counter_data():
    logger.info("保存命令计数器数据")
    fp = cfg.command_counter_data_file_path
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(
        command_counter_data.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
    )
