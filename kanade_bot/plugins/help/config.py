from pathlib import Path

from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import (
    get_plugin_cache_dir,
    get_plugin_config_dir,
    get_plugin_config_file,
)

DEFAULT_WELCOME_MESSAGE_TEMPLATES = [
    "嗯…欢迎{nickname}。我是K，请多指教。",
    "…{nickname}，欢迎你。这里不吵，但如果你需要说话，我会在的。",
    "欢迎{nickname}。我刚好在泡面…有点失礼，不过很高兴认识你。",
    "啊，{nickname}来了。不用太拘束，安静待着也可以。",
    "欢迎{nickname}。我可能话不多，但会一直听着。",
]


class ScopedConfig(BaseModel):
    online_notice_group_ids: list[int] = []
    """Bot上线通知的群聊ID列表，配置后会在Bot上线时发送通知到这些群聊"""

    ntfy_topic_url: str | None = None
    """ntfy的topic的完整URL，配置后会在Bot掉线时发送通知到这个topic，不配置则禁用掉线通知"""
    login_qrcode_file_path: Path | None = None
    """登录二维码的文件路径，配置后会在掉线推送中附带"""

    welcome_message_templates: list[str] = [
        m + "\n如果有什么不清楚的地方…输入 /help 可以看说明。"
        for m in DEFAULT_WELCOME_MESSAGE_TEMPLATES
    ]
    """欢迎新成员入群的消息模板列表，{nickname}会被替换为新成员的昵称"""
    welcome_image_file: str | None = None
    """欢迎新成员入群的图片文件名，未配置则不发送图片"""

    @property
    def docs_dir_path(self) -> Path:
        """帮助文档所在目录路径"""
        return get_plugin_config_dir()

    @property
    def images_cache_dir_path(self) -> Path:
        """帮助文档图片的缓存目录路径"""
        return get_plugin_cache_dir()

    @property
    def welcome_image_file_path(self) -> Path | None:
        """欢迎新成员入群的图片文件路径"""
        if self.welcome_image_file is None:
            return None
        return get_plugin_config_file(self.welcome_image_file)


class Config(BaseModel):
    help: ScopedConfig = ScopedConfig()
