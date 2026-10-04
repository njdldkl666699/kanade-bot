import asyncio
import os
import posixpath
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from mirage import MountMode, Workspace
from mirage.agents.pydantic_ai import PydanticAIWorkspace
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from mirage.vfs.disk import DiskVFS
from nonebot import get_driver, logger

from ..config import cfg

SANDLOCK_ENV_PATH = "/usr/local/bin:/usr/bin:/bin"
"""sandlock 受限子进程的 PATH：只给常见系统目录，避免把宿主环境整体透传进去。"""

ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""沙箱环境变量名的合法形态（防 shell 注入）"""

LANDLOCK_REQUIRED_ABI = 6
"""sandlock 要求的最低 Landlock ABI（Linux 6.12+）"""

LANDLOCK_MIN_ABI = 4
"""低于此 ABI 连文件系统规则都保不住，必须报错而非降级"""

DEGRADED_PROTECTIONS = (
    "signal-scope",
    "abstract-unix-socket-scope",
    "fs-ioctl-dev",
)
"""ABI v5/v6 才有的三项 protection；降级后不再强制

对应能力：signal 宿主进程 / 连宿主 abstract unix socket / 设备 ioctl。
**文件系统隔离主线不受影响**（靠 Landlock FS rules + mirage VFS）。
"""

LANDLOCK_WRAPPER = """#!/bin/sh
# 注入 Landlock 降级参数后 exec 真正的 sandlock。
# 必须在 "--" 之前插入，否则会被当成被沙箱命令的参数。
sub="$1"; shift
exec "{real_sandlock}" "$sub" \\
    --allow-degraded signal-scope \\
    --allow-degraded abstract-unix-socket-scope \\
    --allow-degraded fs-ioctl-dev \\
    "$@"
"""


class SandlockUnavailableError(RuntimeError):
    """sandlock CLI 缺失或不可用"""


class LandlockUnavailableError(RuntimeError):
    """Landlock 不可用或 ABI 过低，无法保证文件系统隔离"""


def _parse_abi(text: str) -> int | None:
    """从 `sandlock check` 输出里解析 Landlock ABI 版本号"""
    m = re.search(r"Landlock:\s*ABI\s*v(\d+)", text)
    return int(m.group(1)) if m else None


def check_sandlock() -> int:
    """校验 sandlock CLI 可用，返回宿主 Landlock ABI 版本

    缺失 CLI 或 Landlock 完全不可用即报错。
    """
    if shutil.which("sandlock") is None and not cfg.sandbox.landlock_real_binary:
        raise SandlockUnavailableError(
            "沙箱需要 sandlock CLI 在 PATH 上（https://github.com/multikernel/sandlock）。"
            "请安装后重启；如需关闭沙箱，可将 chat.sandbox.enabled 设为 false"
        )

    try:
        proc = subprocess.run(
            ["sandlock", "check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandlockUnavailableError(f"执行 sandlock check 失败: {e}") from e

    output = f"{proc.stdout}\n{proc.stderr}"
    abi = _parse_abi(output)
    if abi is None:
        raise LandlockUnavailableError(
            f"无法从 sandlock check 输出解析 Landlock ABI，沙箱不可用:\n{output.strip()}"
        )
    if abi < LANDLOCK_MIN_ABI:
        raise LandlockUnavailableError(
            f"宿主 Landlock ABI 仅 v{abi}（需要 ≥ v{LANDLOCK_MIN_ABI}），"
            f"文件系统隔离无法保证，拒绝启动沙箱"
        )
    return abi


def ensure_landlock(bin_dir: Path) -> int | None:
    """按 ABI 情况决定是否生成降级 wrapper，返回宿主 ABI（未启用降级时为 None）"""
    abi = check_sandlock()
    mode = cfg.sandbox.landlock_degrade

    if mode == "strict":
        if abi < LANDLOCK_REQUIRED_ABI:
            raise LandlockUnavailableError(
                f"宿主 Landlock ABI v{abi} < 要求的 v{LANDLOCK_REQUIRED_ABI}，"
                f"且 landlock_degrade=strict（不降级）"
            )
        return None

    if abi >= LANDLOCK_REQUIRED_ABI and mode != "always":
        logger.info(f"Landlock ABI v{abi} 满足 sandlock 要求（v{LANDLOCK_REQUIRED_ABI}），无需降级")
        return None

    real = cfg.sandbox.landlock_real_binary or shutil.which("sandlock")
    if real is None:
        raise SandlockUnavailableError("找不到 sandlock 可执行文件，无法生成降级 wrapper")

    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "sandlock"
    wrapper.write_text(LANDLOCK_WRAPPER.format(real_sandlock=real), encoding="utf-8")
    wrapper.chmod(0o755)

    # 自检：确认降级确实生效（没有 protection unavailable 报错）
    probe = subprocess.run(
        [str(wrapper), "run", "--allow-degraded", "--", "true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    combined = f"{probe.stdout}\n{probe.stderr}".lower()
    if probe.returncode != 0 or "protection unavailable" in combined:
        raise LandlockUnavailableError(
            f"Landlock 降级 wrapper 自检失败（returncode={probe.returncode}），"
            f"沙箱拒绝启动:\n{(probe.stdout + probe.stderr).strip()}"
        )

    logger.warning(
        f"Landlock ABI v{abi} < v{LANDLOCK_REQUIRED_ABI}，已启用降级 wrapper "
        f"({wrapper})。失去的保护：{'、'.join(DEGRADED_PROTECTIONS)}"
        f"（防进程逃逸维度）；文件系统隔离与内存限额不受影响。"
    )
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    logger.info(f"已将沙箱降级 wrapper 目录前置到 PATH: {bin_dir}")
    return abi


class KanadeWorkspace(PydanticAIWorkspace):
    """Mirage 官方 `PydanticAIWorkspace` 的工作区根绑定版

    官方实现见 `mirage.agents.pydantic_ai.backend`：文件操作（read/write/
    edit/ls）直通 VFS Ops 层、按**虚拟绝对路径**寻址，shell 操作（execute/
    grep/glob）走 `Workspace.shell()`。

    本项目采用 mirage「免 FUSE」布局：挂载前缀 = 宿主真实路径，虚拟路径与
    真实路径一致，sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。
    该布局下官方 backend 收到**相对路径**会落到 VFS 根 `/`（宿主系统视图的
    overlay）——读则 FileNotFoundError，写则**静默落空**（宿主工作区无文件）。
    这里把相对路径统一绑定到工作区根，与 `execute` 的 session cwd（初始化为
    工作区根）语义对齐。

    另补上官方实现缺失的 `execute` 超时（官方接受 `timeout` 参数但不生效）。
    """

    def __init__(self, workspace: Workspace, root: str, session_id: str | None = None) -> None:
        super().__init__(workspace, sandbox_id="mirage", session_id=session_id)
        self._root = root

    def _bind(self, path: str) -> str:
        """相对路径挂到工作区根，绝对路径原样归一化

        归一化后逃出工作区根的路径（`../..` 等）无需专门拦截：VFS 里只有
        工作区一个挂载，界外路径不存在，操作自然失败。
        """
        if not path.startswith("/"):
            path = posixpath.join(self._root, path)
        return posixpath.normpath(path)

    async def aexecute(self, command: str, timeout: int | None = None):
        """执行命令；官方实现忽略 timeout，这里补上（超时取消整个 shell 协程）"""
        if timeout is None or timeout <= 0:
            return await super().aexecute(command)
        async with asyncio.timeout(timeout):
            return await super().aexecute(command)

    async def aread_bytes(self, path: str) -> bytes:
        return await super().aread_bytes(self._bind(path))

    async def aexists(self, path: str) -> bool:
        return await super().aexists(self._bind(path))

    async def aread(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return await super().aread(self._bind(path), offset, limit)

    async def awrite(self, path: str, content: str | bytes):
        return await super().awrite(self._bind(path), content)

    async def aedit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ):
        return await super().aedit(self._bind(path), old_string, new_string, replace_all)

    async def als_info(self, path: str):
        return await super().als_info(self._bind(path))

    async def agrep_raw(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        ignore_hidden: bool = True,
    ):
        # 官方默认搜 VFS 根 '/'，这里改默认搜工作区根
        return await super().agrep_raw(
            pattern, self._bind(path) if path else self._root, glob, ignore_hidden
        )

    async def aglob_info(self, pattern: str, path: str = "/"):
        # 官方默认从 VFS 根 '/' 找，这里改默认从工作区根找
        target = self._bind(path) if path and path != "/" else self._root
        return await super().aglob_info(pattern, target)


@dataclass
class SandboxSession:
    """沙箱：workspace + backend"""

    workspace_dir: Path
    workspace: Workspace
    backend: KanadeWorkspace
    session_id: str
    last_used: float = field(default_factory=time.monotonic)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def root(self) -> str:
        """工作区根的绝对路径"""
        return str(self.workspace_dir)

    async def running(self) -> bool:
        return not self._closed and not self.workspace._shutting_down

    # --- 工具层使用

    def _abs(self, path: Path) -> str:
        """相对路径挂到工作区根的虚拟路径"""
        p = str(path)
        return p if p.startswith("/") else posixpath.join(self.root, p)

    async def read(self, path: Path) -> BytesIO:
        return BytesIO(await self.workspace.vfs.read(self._abs(path), session_id=self.session_id))

    async def write(self, path: Path, data: BytesIO) -> None:
        """写文件（可覆盖），自动逐级创建缺失的父目录"""
        content = data.read()
        target = self._abs(path)
        current = ""
        for part in [p for p in posixpath.dirname(target).split("/") if p]:
            current = f"{current}/{part}"
            try:
                await self.workspace.vfs.mkdir(current, session_id=self.session_id)
            except FileExistsError:
                continue
        await self.workspace.vfs.write(target, content, session_id=self.session_id)


class SandboxManager:
    """沙箱池：惰性创建、空闲TTL/LRU回收"""

    def __init__(self):
        self._sandboxes: dict[str, SandboxSession] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None
        self._bin_dir: Path | None = None

        self._root_dir = cfg.sandbox.workspace_dir_path.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)

        driver = get_driver()
        driver.on_startup(self._startup)
        driver.on_shutdown(self._shutdown)

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

    def _build_runtime(self, workspace_dir: Path) -> SandlockRuntime:
        """构造 sandlock 运行时：只委派 python3，其余命令走 mirage 内置实现"""
        return SandlockRuntime(
            captures=("python3",),
            config={
                # 只授予工作区本身：工作区之外的宿主路径 sandlock 一律拒绝
                "fs_readable": (str(workspace_dir),),
                "fs_writable": (str(workspace_dir),),
                "max_memory": cfg.sandbox.memory_limit,
                "env": {"PATH": SANDLOCK_ENV_PATH},
            },
        )

    # ===== 获取与销毁 =====

    async def acquire(self, session_id: str) -> SandboxSession:
        """获取会话对应的沙箱（惰性创建），刷新使用时间

        工作区根的绝对路径由 `workspace_root(session_id)` 单独查询。
        """
        async with self._lock:
            managed = self._sandboxes.get(session_id)
            if managed is not None and await managed.running():
                managed.last_used = time.monotonic()
                return managed

            managed = await self._create(session_id)
            self._sandboxes[session_id] = managed
            logger.info(
                f"已为会话{session_id}创建沙箱"
                f"（工作区{managed.root}，当前共存{len(self._sandboxes)}个）"
            )
            # 超上限时LRU淘汰（不含刚创建的这个）
            await self._evict_over_limit(exclude=session_id)
            return managed

    def workspace_root(self, session_id: str) -> str:
        """会话工作区根的绝对路径"""
        return str(self._workspace_dir(session_id))

    async def _create(self, session_id: str) -> SandboxSession:
        """构建 Workspace + mirage 会话 + 官方 Pydantic AI backend

        挂载前缀 = DiskVFS 自己的宿主 realpath：虚拟路径与真实路径一致，
        sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。

        会话初始化（cd + export）走 shell 命令：不传 per-call `cwd` 时命令
        直接跑在持久 session 上，`cd`/`export` 会留在 session 状态里，
        之后的 `execute` 即以工作区根为 cwd、带配置的环境变量。
        """
        workspace_dir = self._workspace_dir(session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

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
        )

    async def destroy(self, session_id: str) -> None:
        """销毁会话沙箱"""
        async with self._lock:
            managed = self._sandboxes.pop(session_id, None)
        if managed is None:
            return
        await self._close(managed)

    async def _close(self, managed: SandboxSession) -> None:
        managed._closed = True
        try:
            await managed.workspace.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭沙箱工作区时发生错误: {e}")

    async def destroy_all(self) -> None:
        """销毁全部沙箱"""
        async with self._lock:
            managed_list = list(self._sandboxes.values())
            self._sandboxes.clear()
        for managed in managed_list:
            await self._close(managed)

    # ===== 回收策略 =====

    async def _evict_over_limit(self, *, exclude: str | None = None) -> None:
        """超过最大存活数时LRU淘汰"""
        limit = cfg.sandbox.max_concurrent_sandboxes
        candidates = [(sid, m) for sid, m in self._sandboxes.items() if sid != exclude]
        candidates.sort(key=lambda item: item[1].last_used)
        overflow = len(candidates) + (1 if exclude else 0) - limit
        for sid, managed in candidates[: max(overflow, 0)]:
            self._sandboxes.pop(sid, None)
            logger.info(f"沙箱数量超过上限{limit}，LRU关闭会话{sid}的沙箱")
            await self._close(managed)

    async def _sweep_once(self) -> None:
        """回收空闲超时的沙箱"""
        now = time.monotonic()
        timeout_sec = cfg.sandbox.idle_timeout_minutes * 60
        expired = [
            (sid, m) for sid, m in self._sandboxes.items() if now - m.last_used >= timeout_sec
        ]
        for sid, managed in expired:
            self._sandboxes.pop(sid, None)
            logger.info(f"会话{sid}的沙箱空闲超过{cfg.sandbox.idle_timeout_minutes}分钟，关闭")
            await self._close(managed)

    async def _sweeper_loop(self) -> None:
        while True:
            await asyncio.sleep(cfg.sandbox.sweeper_interval_minutes * 60)
            try:
                await self._sweep_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"沙箱回收任务异常: {e}")

    # ===== 生命周期 =====

    async def _startup(self):
        self._bin_dir = self._root_dir / ".bin"
        ensure_landlock(self._bin_dir)
        logger.info(f"mirage沙箱已就绪，工作区根目录: {self._root_dir}")
        self._sweeper_task = asyncio.get_running_loop().create_task(self._sweeper_loop())

    async def _shutdown(self):
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        await self.destroy_all()
