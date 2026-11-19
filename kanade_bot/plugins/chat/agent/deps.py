from dataclasses import dataclass, field

from pydantic_ai import RunUsage

from kanade_bot.utils.session import SessionInfo

from .memory import MemoryContext, MemoryStore
from .sandbox import SandboxSession


@dataclass
class ChatDeps:
    """一次聊天运行绑定的本地上下文"""

    session_info: SessionInfo
    """当前发送者与会话信息"""

    memory_context: MemoryContext
    """当前发送者对应的记忆作用域上下文"""

    memory_store: MemoryStore
    """长期记忆存储句柄：记忆工具经 deps 访问，无需翻 manager 单例"""

    bot_id: str
    """Bot实例ID"""

    sandbox: SandboxSession | None = None
    """当前聊天会话绑定的沙箱会话"""

    sandbox_root: str | None = None
    """沙箱工作区根的绝对路径"""

    waived_usage: RunUsage = field(default_factory=RunUsage)
    """本轮被豁免计费的usage：重置/压缩后首请求免费额度在本轮内累计，轮末从计费量中扣除"""

    extra: dict = field(default_factory=dict)
    """扩展数据槽"""
