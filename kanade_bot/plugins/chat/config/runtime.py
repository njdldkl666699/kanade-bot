"""聊天运行时配置：黑名单与主动回复启用群"""

from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.schema import AttrDocModel, generate_schema


class ChatConfig(AttrDocModel):
    """聊天配置"""

    banned_users: set[str] = set()
    """拉黑的用户ID列表"""
    banned_groups: set[str] = set()
    """拉黑的群ID列表"""
    auto_reply_enabled_groups: set[str] = set()
    """启用主动回复的群ID列表"""


class ChatConfigs(AttrDocModel):
    """聊天配置文件"""

    console: ChatConfig = ChatConfig()
    onebot: ChatConfig = ChatConfig()

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


generate_schema(ChatConfigs)
