"""mirage 沙箱（Pydantic AI `WorkspaceBackend` 后端）。

每个聊天会话对应一个 mirage Workspace：虚拟文件系统 + shell + 运行时全部在
bot 进程内，native 进程（python3）交给 sandlock 用 Landlock/seccomp 约束。
相比 Docker 容器，每会话内存约 0.1MB（实测 5 会话：140.3MB → 144.7MB），
且没有 docker daemon 依赖与容器冷启动。

`MirageBackend` 把 mirage Workspace 适配到 Pydantic AI 的 `WorkspaceBackend`
协议，隔离语义完全沿用现状（已实测）：

- 工作区外路径**不存在**（VFS 隔离），模型读不到 bot 的 config.yaml / 源码；
- python3 经 sandlock（Landlock + seccomp）约束，`max_memory` 生效；
- 挂载前缀 = 宿主 realpath，免 FUSE。

规模控制沿用两层策略（进程内沙箱没有容器级资源限额）：

1. 空闲 TTL：超过 `idle_timeout_minutes` 未使用的工作区被关闭；
2. LRU 上限：同时存活数超过 `max_concurrent_sandboxes` 时淘汰最久未使用的；
   单个 native 进程的内存由 sandlock 的 `memory_limit` 约束。

注意 `MountMode` 必须是 EXEC 而非 WRITE：WRITE 下 python3 会以
`not in EXEC mode` 失败（exit 126）。
"""

import asyncio
import posixpath
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mirage import MountMode, Workspace
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from mirage.types import FileType
from mirage.utils.errors import NoMountError
from mirage.vfs.disk import DiskVFS
from nonebot import get_driver, logger
from pydantic_ai.workspaces import (
    CommandResult,
    FileEntry,
    WorkspaceCommand,
    WorkspaceRef,
    WorkspaceTimeoutError,
    WorkspaceUnavailableError,
)

from ..config import cfg

SANDLOCK_ENV_PATH = "/usr/local/bin:/usr/bin:/bin"
"""sandlock 受限子进程的 PATH：只给常见系统目录，避免把宿主环境整体透传进去。"""

_MAX_SYMLINKS = 40
"""realpath 跟随的符号链接上限，与 Linux MAXSYMLINKS 一致（超出即报 ELOOP）"""

_ELOOP = 40
"""POSIX ELOOP：Too many levels of symbolic links"""

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
# 用 `sub=$1; shift` 而非 "$@" 切片：${{@:2}} 是 bash 扩展，/bin/sh(dash) 不支持，
# 会静默失效（沙箱看似运行、实际未降级）。
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


def _decode(raw: bytes | str) -> str:
    """字节输出转文本；无法解码的字节替换为 U+FFFD，绝不丢弃"""
    if isinstance(raw, str):
        return raw
    return raw.decode("utf-8", errors="replace")


def check_sandlock() -> int:
    """校验 sandlock CLI 可用，返回宿主 Landlock ABI 版本

    缺失 CLI 或 Landlock 完全不可用即报错（不做静默回退）。
    """
    if shutil.which("sandlock") is None and not cfg.sandbox.landlock_real_binary:
        raise SandlockUnavailableError(
            "沙箱需要 sandlock CLI 在 PATH 上（https://github.com/multikernel/sandlock）。"
            "请安装后重启；如需临时关闭沙箱，可将 chat.sandbox.enabled 设为 false"
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
    """按 ABI 情况决定是否生成降级 wrapper，返回宿主 ABI（未启用降级时为 None）

    - ABI ≥ v6 → 用真 sandlock，无需 wrapper；
    - v4 ≤ ABI < v6 → 生成 wrapper 注入 `--allow-degraded`，并在日志中
      明确打印失去的保护；
    - ABI < v4 → `check_sandlock` 已报错，不会走到这里。

    生成后**自检**一次：用 wrapper 跑一条带 v6 protection 的命令，
    确认没有报「protection unavailable」。否则降级没生效却看不出来，
    是最危险的失败模式。
    """
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
    # mirage 用 shutil.which("sandlock") 查找，PATH 前置让它们命中 wrapper。
    # 注意 wrapper 内部的 exec 用的是绝对路径（子进程 PATH 被 --clean-env 清空）。
    import os

    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    logger.info(f"已将沙箱降级 wrapper 目录前置到 PATH: {bin_dir}")
    return abi


@dataclass
class MirageBackend:
    """mirage Workspace → Pydantic AI `WorkspaceBackend` 协议适配

    实现 `SupportsCommands` + `SupportsFilesystem` + `SupportsRealpath`，
    因此 `Workspace` 会把文件操作直接交给 VFS，而不是退化成 shell 实现。
    """

    workspace: Workspace
    """底层 mirage Workspace"""

    root: str
    """工作区根的绝对路径（宿主 realpath，与虚拟路径一致）"""

    session_id: str
    """mirage 会话 ID（shell 状态的宿主）"""

    environment: dict[str, str] = field(default_factory=dict)
    """注入到每个命令的环境变量"""

    _ref: WorkspaceRef | None = field(default=None, init=False)
    _closed: bool = field(default=False, init=False)

    # ===== WorkspaceBackend =====

    @property
    def ref(self) -> WorkspaceRef | None:
        return self._ref

    @property
    def read_only(self) -> bool:
        return False

    def durable_policy(self) -> tuple[object, ...]:
        return ()

    async def working_dir(self) -> str:
        return self.root

    # ===== SupportsRealpath =====

    async def realpath(self, path: str) -> str:
        """在 VFS 内逐段解析符号链接

        不能用宿主 `Path.resolve()`：VFS 里的链接在宿主目录未必存在，
        且工作区外的路径在 VFS 中根本不存在。

        必须**逐段**解析（而不是只看末段）才能处理 `link/missing` 这种形态：
        链接在中间，结果要一路跟到底。
        """
        return await self._realpath(self._abs(path), _MAX_SYMLINKS)

    async def _realpath(self, path: str, budget: int) -> str:
        segments = [s for s in path.split("/") if s not in ("", ".")]
        resolved: list[str] = []
        for index, segment in enumerate(segments):
            if segment == "..":
                if resolved:
                    resolved.pop()
                continue
            candidate = "/" + "/".join([*resolved, segment])
            try:
                target = await self.workspace.vfs.readlink(candidate, session_id=self.session_id)
            except OSError:
                resolved.append(segment)
                continue

            budget -= 1
            if budget <= 0:
                raise OSError(_ELOOP, "Too many levels of symbolic links", path)
            # 相对目标从链接**所在目录**（即 resolved，不含链接名自身）起算
            if target.startswith("/"):
                base = target
            else:
                base = "/" + "/".join([*resolved, target])
            rest = segments[index + 1 :]
            return await self._realpath(posixpath.join(base, *rest), budget)
        return "/" + "/".join(resolved)

    @staticmethod
    def _normalize(path: str) -> str:
        """纯文本归一化：解析 `.`/`..`，不消除符号链接"""
        parts: list[str] = []
        for segment in path.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(segment)
        return "/" + "/".join(parts)

    # ===== SupportsCommands =====

    async def run(
        self,
        command: WorkspaceCommand,
        *,
        shell: bool = False,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        """在工作区里执行命令，返回完整输出

        `command` 为字符串时必须 `shell=True`，为 argv 序列时必须 `shell=False`；
        不匹配抛 `TypeError`（官方 `WorkspaceBackendSuite` 的硬契约）。

        非零退出码是**正常结果**（不是异常）；超时抛 `WorkspaceTimeoutError`；
        工作区已销毁抛 `WorkspaceUnavailableError`。
        """
        if isinstance(command, str):
            if not shell:
                raise TypeError(
                    "a string command requires shell=True; an argv sequence requires shell=False"
                )
            line = command
        else:
            if shell:
                raise TypeError(
                    "an argv sequence requires shell=False; a string command requires shell=True"
                )
            if not command:
                raise ValueError("command must not be empty")
            line = shlex.join(command)

        merged_env = {**self.environment, **dict(env or {})}

        try:
            if timeout is None:
                io_result = await self.workspace.shell(
                    line,
                    session_id=self.session_id,
                    cwd=self.root,
                    env=merged_env or None,
                )
            else:
                async with asyncio.timeout(timeout):
                    io_result = await self.workspace.shell(
                        line,
                        session_id=self.session_id,
                        cwd=self.root,
                        env=merged_env or None,
                    )
        except TimeoutError as e:
            raise WorkspaceTimeoutError(f"命令执行超时（{timeout}s）: {line}") from e
        except Exception as e:
            if self._closed:
                raise WorkspaceUnavailableError("沙箱工作区已关闭") from e
            raise

        return CommandResult(
            exit_code=io_result.exit_code,
            stdout=_decode(await io_result.materialize_stdout()),
            stderr=_decode(await io_result.materialize_stderr()),
        )

    # ===== SupportsFilesystem =====

    def _abs(self, path: str) -> str:
        """相对路径挂到工作区根下（工作区外的路径在 VFS 里不存在）"""
        return path if path.startswith("/") else posixpath.join(self.root, path)

    async def read_bytes(self, path: str) -> bytes:
        return await self.workspace.vfs.read(self._abs(path), session_id=self.session_id)

    async def write_bytes(self, path: str, data: bytes) -> None:
        """写文件，自动创建缺失的父目录，并穿过符号链接写"""
        target = self._abs(path)
        parent = posixpath.dirname(target) or "/"
        await self._check_dir(parent)
        if not await self.exists(parent):
            await self.make_dir(parent)
        await self.workspace.vfs.write(target, data, session_id=self.session_id)

    async def _check_dir(self, path: str) -> None:
        """确认路径不是已存在的普通文件（是文件则抛 NotADirectoryError）"""
        try:
            st = await self.workspace.vfs.stat(path, session_id=self.session_id)
        except (FileNotFoundError, NoMountError):
            return  # 不存在的父目录由写入方自己创建
        if st.type != FileType.DIRECTORY:
            raise NotADirectoryError(f"不是目录: {path}")

    async def stat(self, path: str) -> FileEntry:
        st = await self.workspace.vfs.stat(self._abs(path), session_id=self.session_id)
        return FileEntry(
            name=st.name or posixpath.basename(path),
            path=self._abs(path),
            is_dir=st.type == FileType.DIRECTORY,
            size=st.size,
        )

    async def list_dir(self, path: str) -> Sequence[FileEntry]:
        """列出目录项（非递归）

        mirage 的 `readdir` 返回的是**完整路径**而非名字，这里取 basename 作为
        `name`，并直接用完整路径拼 `path`。
        """
        target = self._abs(path)
        entries = []
        for child in await self.workspace.vfs.readdir(target, session_id=self.session_id):
            entries.append(
                FileEntry(
                    name=posixpath.basename(child),
                    path=child,
                    is_dir=await self._is_dir(child),
                    size=await self._size(child),
                )
            )
        return entries

    async def _is_dir(self, path: str) -> bool:
        """是否为目录（跟随符号链接；循环链接退回链接自身，判为非目录）"""
        try:
            st = await self.workspace.vfs.stat(path, session_id=self.session_id)
        except OSError:
            try:
                st = await self.workspace.vfs.stat(path, nofollow=True, session_id=self.session_id)
            except OSError:
                return False
        return st.type == FileType.DIRECTORY

    async def _size(self, path: str) -> int | None:
        try:
            st = await self.workspace.vfs.stat(path, session_id=self.session_id)
        except OSError:
            return None
        return st.size

    async def make_dir(self, path: str) -> None:
        target = self._abs(path)
        # 目标已存在：目录视为成功（mkdir -p），文件则抛 FileExistsError
        try:
            st = await self.workspace.vfs.stat(target, session_id=self.session_id)
        except (FileNotFoundError, NoMountError):
            st = None
        else:
            if st.type == FileType.DIRECTORY:
                return
            raise FileExistsError(f"已存在且不是目录: {target}")

        # 逐级创建（mkdir -p 语义），但父路径是文件时报 NotADirectoryError
        parts = [p for p in target.split("/") if p]
        current = ""
        for part in parts:
            current = f"{current}/{part}"
            await self._check_dir(posixpath.dirname(current) or "/")
            try:
                await self.workspace.vfs.mkdir(current, session_id=self.session_id)
            except FileExistsError:
                continue

    async def remove(self, path: str) -> None:
        """删除文件或目录树；符号链接删的是链接本身，不跟随

        走 shell 的 `rm -rf`：VFS 逐层递归遇到循环链接会死循环，
        而 `rm -rf` 天然不跟随符号链接。
        """
        target = self._abs(path)
        if target == self.root or self.root.startswith(target.rstrip("/") + "/"):
            raise ValueError(f"拒绝删除工作区根或其祖先: {path}")
        if not await self.exists(target):
            raise FileNotFoundError(f"路径不存在: {path}")
        result = await self.run(["rm", "-rf", target])
        if result.exit_code != 0:
            raise OSError(result.stderr.strip() or f"删除失败: {path}")

    async def exists(self, path: str) -> bool:
        try:
            await self.workspace.vfs.stat(self._abs(path), session_id=self.session_id)
        except (FileNotFoundError, NotADirectoryError, NoMountError):
            return False
        return True


@dataclass
class SandboxSession:
    """一个聊天会话的沙箱：workspace + backend"""

    workspace_dir: Path
    workspace: Workspace
    backend: MirageBackend
    last_used: float = field(default_factory=time.monotonic)

    @property
    def root(self) -> str:
        """工作区根的绝对路径（模型需要用它拼绝对路径）"""
        return str(self.workspace_dir)

    async def running(self) -> bool:
        return not self.backend._closed and not self.workspace._shutting_down

    # 兼容工具层（tool.py 直接用沙箱读写文件）
    async def read(self, path: Path) -> Any:
        import io

        return io.BytesIO(await self.backend.read_bytes(str(path)))

    async def write(self, path: Path, data: Any) -> None:
        content = data.read()
        if isinstance(content, str):
            content = content.encode("utf-8")
        await self.backend.write_bytes(str(path), content)


class SandboxManager:
    """聊天会话的沙箱池：惰性创建、空闲TTL/LRU回收"""

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
        """会话对应的宿主工作区目录

        必须 `resolve()`：`/tmp` 在多数系统上是 symlink，不解析会让虚拟挂载
        前缀与真实路径不一致，导致写入落进 workspace overlay 而宿主目录为空。
        """
        return (self._root_dir / self._safe_name(session_id)).resolve()

    def delete_workspace(self, session_id: str) -> None:
        """删除会话的工作区目录（重置会话时调用）"""
        workspace_dir = self._workspace_dir(session_id)
        if workspace_dir.parent != self._root_dir:
            logger.warning(f"拒绝删除沙箱目录（不在根目录下）: {workspace_dir}")
            return
        shutil.rmtree(workspace_dir, ignore_errors=True)

    # ===== mirage 运行时 =====

    def _build_runtime(self, workspace_dir: Path) -> SandlockRuntime:
        """构造 sandlock 运行时：只委派 python3，其余命令走 mirage 内置实现

        不捕获 `@external`：让 mirage 内置的 cat/grep/ls/echo/sed/find/curl 在
        VFS 内执行（相对路径正常，且经过 mirage 的策略与观测管线）。只把 python3
        交给宿主 CPython，才能拿到完整 stdlib 与三方库。
        """
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
        """会话工作区根的绝对路径（供系统提示词告知模型）"""
        return str(self._workspace_dir(session_id))

    async def _create(self, session_id: str) -> SandboxSession:
        """构建 Workspace + mirage 会话 + WorkspaceBackend

        挂载前缀 = DiskVFS 自己的宿主 realpath：虚拟路径与真实路径一致，
        sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。
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

        backend = MirageBackend(
            workspace=workspace,
            root=str(workspace_dir),
            session_id=mirage_session,
            environment=dict(cfg.sandbox.environment),
        )
        backend._ref = WorkspaceRef(provider="mirage", id=self._safe_name(session_id))

        return SandboxSession(
            workspace_dir=workspace_dir,
            workspace=workspace,
            backend=backend,
        )

    async def destroy(self, session_id: str) -> None:
        """销毁会话沙箱（保留工作区目录，下次 acquire 直接复用）"""
        async with self._lock:
            managed = self._sandboxes.pop(session_id, None)
        if managed is None:
            return
        await self._close(managed)

    async def _close(self, managed: SandboxSession) -> None:
        managed.backend._closed = True
        try:
            await managed.workspace.close()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭沙箱工作区时发生错误: {e}")

    async def destroy_all(self) -> None:
        """销毁全部沙箱（保留工作区目录）"""
        async with self._lock:
            managed_list = list(self._sandboxes.values())
            self._sandboxes.clear()
        for managed in managed_list:
            await self._close(managed)

    # ===== 回收策略 =====

    async def _evict_over_limit(self, *, exclude: str | None = None) -> None:
        """超过最大存活数时LRU淘汰（工作区目录保留）"""
        limit = cfg.sandbox.max_concurrent_sandboxes
        candidates = [(sid, m) for sid, m in self._sandboxes.items() if sid != exclude]
        candidates.sort(key=lambda item: item[1].last_used)
        overflow = len(candidates) + (1 if exclude else 0) - limit
        for sid, managed in candidates[: max(overflow, 0)]:
            self._sandboxes.pop(sid, None)
            logger.info(f"沙箱数量超过上限{limit}，LRU关闭会话{sid}的沙箱（工作区保留）")
            await self._close(managed)

    async def _sweep_once(self) -> None:
        """回收空闲超时的沙箱（工作区目录保留）"""
        now = time.monotonic()
        timeout_sec = cfg.sandbox.idle_timeout_minutes * 60
        expired = [
            (sid, m) for sid, m in self._sandboxes.items() if now - m.last_used >= timeout_sec
        ]
        for sid, managed in expired:
            self._sandboxes.pop(sid, None)
            logger.info(
                f"会话{sid}的沙箱空闲超过{cfg.sandbox.idle_timeout_minutes}分钟，关闭（工作区保留）"
            )
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
