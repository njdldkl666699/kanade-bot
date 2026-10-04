from pathlib import Path

from nonebot import require

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_config_file

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
