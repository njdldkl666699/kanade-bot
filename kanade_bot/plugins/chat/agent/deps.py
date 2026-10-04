from dataclasses import dataclass, field

from kanade_bot.utils.session import SessionInfo

from .memory import MemoryContext
from .sandbox import SandboxSession


@dataclass
class ChatDeps:
    """一次聊天运行绑定的本地上下文"""

    session_info: SessionInfo
    """当前发送者与会话信息"""

    memory_context: MemoryContext
    """当前发送者对应的记忆作用域上下文"""

    bot_id: str | None = None
    """OneBot Bot实例ID"""

    sandbox: SandboxSession | None = None
    """当前聊天会话绑定的沙箱会话"""

    sandbox_root: str | None = None
    """沙箱工作区根的绝对路径"""

    extra: dict = field(default_factory=dict)
    """扩展数据槽"""
