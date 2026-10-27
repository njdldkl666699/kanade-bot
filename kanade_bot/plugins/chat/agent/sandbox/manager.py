"""沙箱管理器：为每个会话维护一个常驻沙箱，工作区目录按会话管理"""

import shlex
import shutil
from pathlib import Path

from mirage import DiskVFS, MountMode, Workspace
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from nonebot import get_driver, logger

from ...config import cfg
from .host_env import (
    ENV_KEY_RE,
    SANDBOX_HOME_DIR,
    UvUnavailableError,
    check_uv,
    ensure_landlock,
    resolve_font_dirs,
    resolve_host_python,
)
from .session import SandboxSession
from .workspace import KanadeWorkspace, sandbox_runtime_config


class SandboxManager:
    """沙箱管理器：为每个会话维护一个常驻沙箱，工作区目录按会话管理"""

    def __init__(self):
        self._bin_dir: Path | None = None

        self._root_dir = cfg.sandbox.workspace_dir_path.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)

        self._sessions: dict[str, SandboxSession] = {}
        """各会话的常驻沙箱，键为会话ID"""

        self._uv_bin: str | None = None
        """宿主侧 uv 可执行文件路径"""

        self._uv_python_dir: Path | None = None
        """额外只读授权的解释器目录"""

        self._host_python: str | None = None
        """宿主系统 python3 路径"""

        self._font_dirs: tuple[str, ...] = tuple(resolve_font_dirs())
        """授权沙箱只读的宿主字体目录"""

        driver = get_driver()
        driver.on_startup(self._startup)

    # ===== 路径 =====

    @staticmethod
    def _safe_name(session_id: str) -> str:
        """会话ID转单一安全路径段"""
        name = session_id.replace("/", "_").replace("\\", "_")
        if name in {"", ".", ".."}:
            raise ValueError(f"会话ID无法转换为安全目录名: {session_id!r}")
        return name

    def _workspace_dir(self, session_id: str) -> Path:
        """会话对应的宿主工作区目录"""
        return (self._root_dir / self._safe_name(session_id)).resolve()

    def delete_workspace(self, session_id: str) -> None:
        """删除会话的工作区目录"""
        workspace_dir = self._workspace_dir(session_id)
        if workspace_dir.parent != self._root_dir:
            logger.warning(f"拒绝删除沙箱目录（不在根目录下）: {workspace_dir}")
            return
        shutil.rmtree(workspace_dir, ignore_errors=True)

    # ===== mirage 运行时 =====

    def _check_uv(self) -> None:
        """解析 uv 与托管解释器目录（正常由启动钩子执行）"""
        self._uv_bin, self._uv_python_dir = check_uv()

    def _resolve_host_python(self) -> None:
        """解析无 uv 时的原生回退解释器；安装根在系统目录外时补进只读授权"""
        self._host_python, extra_dir = resolve_host_python(self._uv_python_dir)
        if extra_dir is not None:
            self._uv_python_dir = extra_dir

    def _build_runtime(self, workspace_dir: Path):
        """构造 sandlock 运行时"""
        return SandlockRuntime(
            captures=("python3", "python"),
            config=sandbox_runtime_config(workspace_dir, self._uv_python_dir, self._font_dirs),
        )

    # ===== 创建 =====

    async def create(self, session_id: str) -> SandboxSession:
        """获取会话的常驻沙箱，不存在时创建"""
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            return session
        session = await self._create(session_id)
        self._sessions[session_id] = session
        logger.info(f"已为会话{session_id}创建常驻沙箱（工作区{session.root}）")
        return session

    async def close(self, session_id: str) -> None:
        """关闭会话的常驻沙箱（未创建时为空操作）。工作区文件保留在宿主目录"""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        """关闭所有常驻沙箱"""
        sessions = [self._sessions.pop(sid) for sid in list(self._sessions)]
        for session in sessions:
            await session.close()

    def workspace_root(self, session_id: str) -> str:
        """会话工作区根的绝对路径"""
        return str(self._workspace_dir(session_id))

    async def _create(self, session_id: str) -> SandboxSession:
        """构建 Workspace + mirage 会话 + 官方 Pydantic AI backend

        会话初始化（cd 工作区根 + export 环境变量）写入持久会话状态。
        """
        workspace_dir = self._workspace_dir(session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        sandbox_home = workspace_dir / SANDBOX_HOME_DIR
        for sub in (sandbox_home, sandbox_home / ".cache", sandbox_home / ".config"):
            sub.mkdir(parents=True, exist_ok=True)

        workspace = Workspace(
            {
                str(workspace_dir): (
                    DiskVFS(str(workspace_dir)),
                    MountMode.EXEC,
                )
            },
            mode=MountMode.EXEC,
            runtimes=[self._build_runtime(workspace_dir)],
        )
        mirage_session = f"kanade-{self._safe_name(session_id)}"
        workspace.create_session(mirage_session)

        init_parts = [f"cd {shlex.quote(str(workspace_dir))}"]
        for key, value in cfg.sandbox.environment.items():
            if not ENV_KEY_RE.fullmatch(key):
                await workspace.close()
                raise ValueError(f"沙箱环境变量名不合法: {key!r}")
            init_parts.append(f"export {key}={shlex.quote(value)}")

        io = await workspace.shell("; ".join(init_parts), session_id=mirage_session)
        if io.exit_code != 0:
            stderr = (await io.materialize_stderr()).decode("utf-8", "replace")
            await workspace.close()
            raise RuntimeError(f"沙箱会话初始化失败（cd/export）: {stderr.strip()}")

        return SandboxSession(
            workspace_dir=workspace_dir,
            workspace=workspace,
            backend=KanadeWorkspace(
                workspace=workspace,
                root=str(workspace_dir),
                session_id=mirage_session,
            ),
            session_id=mirage_session,
            uv_bin=self._uv_bin,
            uv_python_dir=self._uv_python_dir,
            host_python=self._host_python,
            font_dirs=self._font_dirs,
        )

    # ===== 生命周期 =====

    async def _startup(self):
        self._bin_dir = self._root_dir / ".bin"
        ensure_landlock(self._bin_dir)
        try:
            self._check_uv()
        except UvUnavailableError as e:
            logger.warning(f"{e}")
        self._resolve_host_python()
        if self._uv_bin is None and self._host_python is None:
            logger.warning("宿主侧既无 uv 也无 python3，沙箱内将无法创建 Python 环境")
        logger.info(
            f"mirage沙箱已就绪，工作区根目录: {self._root_dir}，"
            f"uv: {self._uv_bin}，解释器目录: {self._uv_python_dir}，"
            f"回退python: {self._host_python}"
        )
