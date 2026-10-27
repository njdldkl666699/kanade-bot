"""持久化记忆配置"""

from pathlib import Path

from nonebot import require
from pydantic import PositiveInt

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_data_file


class MemoryConfig(AttrDocModel):
    """持久化记忆配置"""

    database_file: str = "memories.sqlite3"
    """持久化记忆数据库文件名，位于插件数据目录下"""

    max_records_per_scope: PositiveInt = 256
    """每个用户或群聊最多保留的记忆条数，超出后淘汰最久未更新的记录"""

    @property
    def database_file_path(self) -> Path:
        return get_plugin_data_file(self.database_file)
