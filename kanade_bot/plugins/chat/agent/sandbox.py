"""mirage 沙箱管理（openai-agents SandboxAgent 后端）。

每个聊天会话对应一个 mirage Workspace：虚拟文件系统 + shell + 运行时全部在
bot 进程内，native 进程（python3）交给 sandlock 用 Landlock/seccomp 约束。
相比 Docker 容器，每会话内存约 0.1MB（实测 5 会话：140.3MB → 144.7MB），
且没有 docker daemon 依赖与容器冷启动。

工作区用 DiskVFS 落到宿主机真实目录，**免 FUSE**：把它的虚拟挂载前缀设成自己的
宿主 realpath，虚拟路径与真实路径一致，sandlock 拉起的进程能直接看到真实文件
（不需要 fuse3/mfusepy）。

规模控制沿用两层策略（进程内沙箱没有容器级资源限额）：

1. 空闲 TTL：超过 `idle_timeout_minutes` 未使用的工作区被关闭；
2. LRU 上限：同时存活数超过 `max_concurrent_sandboxes` 时淘汰最久未使用的；
   单个 native 进程的内存由 sandlock 的 `memory_limit` 约束。

注意 `MountMode` 必须是 EXEC 而非 WRITE：WRITE 下 python3 会以
`not in EXEC mode` 失败（exit 126）。
"""

import asyncio
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from agents.sandbox import Manifest
from agents.sandbox.session import SandboxSession
from mirage import MountMode, Workspace
from mirage.agents.openai_agents import MirageSandboxClient
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from mirage.vfs.disk import DiskVFS
from nonebot import get_driver, logger

from ..config import cfg

SANDLOCK_ENV_PATH = "/usr/local/bin:/usr/bin:/bin"
"""sandlock 受限子进程的 PATH：只给常见系统目录，避免把宿主环境整体透传进去。"""


class SandlockUnavailableError(RuntimeError):
    """sandlock CLI 缺失或不可用"""


@dataclass
class _ManagedSandbox:
    """一个聊天会话的沙箱：workspace + client + session"""

    workspace_dir: Path
    workspace: Workspace
    client: MirageSandboxClient
    session: SandboxSession
    last_used: float = field(default_factory=time.monotonic)

    @property
    def root(self) -> str:
        """工作区根的绝对路径（模型需要用它拼绝对路径）"""
        return str(self.workspace_dir)


class SandboxManager:
    """聊天会话的沙箱池：惰性创建、空闲TTL/LRU回收"""

    def __init__(self):
        self._sandboxes: dict[str, _ManagedSandbox] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None

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

    # ===== sandlock =====

    @staticmethod
    def check_sandlock() -> None:
        """校验 sandlock CLI 可用，缺失即报错（不做静默回退）"""
        if shutil.which("sandlock") is None:
            raise SandlockUnavailableError(
                "沙箱需要 sandlock CLI 在 PATH 上（https://github.com/multikernel/sandlock），"
                "要求 Linux 6.12+（Landlock ABI v6）。请安装后重启；"
                "如需临时关闭沙箱，可将 chat.sandbox.enabled 设为 false"
            )

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
            if managed is not None and await managed.session.running():
                managed.last_used = time.monotonic()
                return managed.session

            managed = await self._create(session_id)
            self._sandboxes[session_id] = managed
            logger.info(
                f"已为会话{session_id}创建沙箱"
                f"（工作区{managed.root}，当前共存{len(self._sandboxes)}个）"
            )
            # 超上限时LRU淘汰（不含刚创建的这个）
            await self._evict_over_limit(exclude=session_id)
            return managed.session

    def workspace_root(self, session_id: str) -> str:
        """会话工作区根的绝对路径（供系统提示词告知模型）"""
        return str(self._workspace_dir(session_id))

    async def _create(self, session_id: str) -> _ManagedSandbox:
        """构建 Workspace + client + session

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
        client = MirageSandboxClient(workspace)
        # manifest.root 指向真实工作区目录，相对路径与绝对路径都落在这里
        session = await client.create(
            manifest=Manifest(root=str(workspace_dir), environment=cfg.sandbox.environment)
        )
        await session.start()

        return _ManagedSandbox(
            workspace_dir=workspace_dir,
            workspace=workspace,
            client=client,
            session=session,
        )

    async def destroy(self, session_id: str) -> None:
        """销毁会话沙箱（保留工作区目录，下次 acquire 直接复用）"""
        async with self._lock:
            managed = self._sandboxes.pop(session_id, None)
        if managed is None:
            return
        await self._close(managed)

    async def _close(self, managed: _ManagedSandbox) -> None:
        try:
            await managed.session.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"关闭沙箱会话时发生错误: {e}")
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
        self.check_sandlock()
        logger.info(f"mirage沙箱已就绪，工作区根目录: {self._root_dir}")
        self._sweeper_task = asyncio.get_running_loop().create_task(self._sweeper_loop())

    async def _shutdown(self):
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        await self.destroy_all()
