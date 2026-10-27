"""工具公共辅助"""

from pydantic_ai import RunContext

from ..deps import ChatDeps
from ..sandbox import SandboxSession


def get_sandbox(ctx: RunContext[ChatDeps]) -> SandboxSession | None:
    """获取当前会话的沙箱，未启用时返回None"""
    return ctx.deps.sandbox
