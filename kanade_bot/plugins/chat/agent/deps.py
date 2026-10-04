"""聊天Agent的运行时依赖（Pydantic AI `deps`）。

`ChatDeps` 实例通过 `agent.run(..., deps=...)` 传入，在工具函数内经
`RunContext[ChatDeps].deps` 访问。每次发送前由会话管理器构建，携带当前发送者
身份（群聊会话被多个成员复用时逐轮切换）与沙箱会话引用。

沙箱工作区本身通过 `ctx.workspace` 访问，因此这里只保留
「是否启用沙箱」之外的应用层状态。
"""

from dataclasses import dataclass, field

from kanade_bot.utils.session import SessionInfo

from .memory import MemoryContext
from .sandbox import SandboxSession


@dataclass
class ChatDeps:
    """一次聊天运行绑定的本地上下文（不发送给模型）"""

    session_info: SessionInfo
    """当前发送者与会话信息"""

    bot_id: str | None = None
    """OneBot Bot实例ID，用于发消息等宿主操作"""

    memory_context: MemoryContext | None = None
    """当前发送者对应的记忆作用域上下文"""

    sandbox: SandboxSession | None = None
    """当前聊天会话绑定的沙箱会话（启用沙箱时由管理器注入）"""

    sandbox_root: str | None = None
    """沙箱工作区根的绝对路径，供系统提示词告知模型（sandlock下python3需绝对路径）"""

    extra: dict = field(default_factory=dict)
    """扩展数据槽"""
