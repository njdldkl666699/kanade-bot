"""聊天 Agent 图片转述 TTS 配置"""

from pathlib import Path

from nonebot import require

from kanade_bot.utils.schema import AttrDocModel, BaseAgentConfig

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
    PromptSectionConfig(file="sandbox_python.md", when="python_env_available"),
    PromptSectionConfig(file="memes.md", when="memes_enabled"),
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


class AgentConfig(BaseAgentConfig):
    """聊天Agent配置"""

    prompt: ChatPromptConfig = ChatPromptConfig()
    """模块化系统提示词配置"""


class ImageCaptionConfig(BaseAgentConfig):
    """图片转述模型配置"""

    system_prompt_file: str = "ImageCaption.md"
    """系统提示词文件名"""

    @property
    def system_prompt_file_path(self) -> Path:
        """系统提示词文件路径"""
        return get_plugin_config_file(self.system_prompt_file)


class TTSConfig(AttrDocModel):
    """文本转语音模型配置"""

    base_url: str | None = None
    """TTS服务的Base URL，如果为None则不启用TTS"""
    model: str | None = None
    """TTS使用的模型名称，不配置则使用服务端默认模型"""
    voice: str | None = None
    """TTS使用的声音类型，不配置则使用服务端默认模型"""
