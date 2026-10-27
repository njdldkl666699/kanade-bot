"""沙箱工作区：mirage 官方 backend 的工作区根绑定与 capability 分发"""

import posixpath
from pathlib import Path
from typing import TYPE_CHECKING

from mirage import Workspace
from mirage.agents.pydantic_ai import MirageWorkspaceBackend
from mirage.runtime.sandbox.sandlock import SandlockConfig
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from ...config import cfg
from .host_env import SANDBOX_HOME_DIR, VENV_DIR

if TYPE_CHECKING:
    from .session import SandboxSession


class KanadeWorkspace(MirageWorkspaceBackend):
    """mirage 官方 backend 的工作区根绑定版：相对路径统一挂到工作区根"""

    def __init__(self, workspace: Workspace, root: str, session_id: str | None = None) -> None:
        super().__init__(workspace, session_id)
        self._root = root

    def _bind(self, path: str) -> str:
        """相对路径挂到工作区根，绝对路径原样归一化"""
        if not path.startswith("/"):
            path = posixpath.join(self._root, path)
        return posixpath.normpath(path)

    async def read_bytes(self, path: str) -> bytes:
        return await super().read_bytes(self._bind(path))

    async def write_bytes(self, path: str, data: bytes) -> None:
        await super().write_bytes(self._bind(path), data)

    async def stat(self, path: str):
        return await super().stat(self._bind(path))

    async def list_dir(self, path: str):
        return await super().list_dir(self._bind(path))

    async def make_dir(self, path: str) -> None:
        await super().make_dir(self._bind(path))

    async def exists(self, path: str) -> bool:
        return await super().exists(self._bind(path))

    async def remove(self, path: str) -> None:
        await super().remove(self._bind(path))

    async def realpath(self, path: str) -> str:
        return await super().realpath(self._bind(path))


def sandbox_runtime_config(
    workspace_dir: Path,
    uv_python_dir: Path | None,
    font_dirs: tuple[str, ...] = (),
) -> SandlockConfig:
    """sandlock 受限子进程的统一授权与环境配置

    只授予工作区、uv 解释器目录与字体目录，之外的宿主路径 sandlock 一律拒绝。
    """
    readable = [str(workspace_dir)]
    if uv_python_dir is not None:
        readable.append(str(uv_python_dir))
    readable.extend(font_dirs)
    sandbox_home = workspace_dir / SANDBOX_HOME_DIR
    return SandlockConfig(
        fs_readable=tuple(readable),
        fs_writable=(str(workspace_dir),),
        max_memory=cfg.sandbox.memory_limit,
        env={
            "PATH": str(workspace_dir / VENV_DIR / "bin"),
            "HOME": str(sandbox_home),
            "XDG_CACHE_HOME": str(sandbox_home / ".cache"),
            "XDG_CONFIG_HOME": str(sandbox_home / ".config"),
        },
    )


class SandboxWorkspaceCapability(AbstractCapability[AgentDepsT]):
    """按运行时分发沙箱工作区的 capability"""

    def get_workspace(
        self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        sandbox: SandboxSession | None = getattr(ctx.deps, "sandbox", None)
        if sandbox is None or sandbox.closed:
            return None
        return sandbox.backend
