"""Docker 沙箱管理（openai-agents SandboxAgent 后端）。

每个聊天会话对应一个沙箱会话（Docker 容器 + 工作区）。内存受限环境下通过
三层策略控制占用：

1. 空闲 TTL：超过 `idle_timeout_minutes` 未使用的容器被销毁（销毁前快照
   保留工作区，下次消息时从快照恢复）；
2. LRU 上限：同时存活容器数超过 `max_concurrent_containers` 时淘汰最久
   未使用的；
3. 资源限额：单容器 mem_limit/cpus（SDK 的
   `DockerSandboxClientOptions` 不暴露这些参数，通过子类在容器创建后
   调用 docker update 注入）。
"""

import asyncio
import time
from dataclasses import dataclass, field

from agents.sandbox import Manifest
from agents.sandbox.sandboxes.docker import (
    DockerSandboxClient,
    DockerSandboxClientOptions,
)
from agents.sandbox.session import SandboxSession
from agents.sandbox.snapshot import LocalSnapshot
from nonebot import get_driver, logger

from ..config import cfg


@dataclass(frozen=True)
class ResourceLimits:
    """单容器资源限额"""

    mem_limit: str = "256m"
    cpus: float = 1.0


class KanadeDockerClient(DockerSandboxClient):
    """注入资源限额的Docker沙箱客户端

    SDK 的 `DockerSandboxClientOptions` 只有 image/exposed_ports/network_mode/
    labels，无资源限制参数；这里在容器创建后调用 docker update 补充。
    """

    def __init__(self, docker_client, resource_limits: ResourceLimits | None = None):
        super().__init__(docker_client)
        self._resource_limits = resource_limits

    async def _create_container(self, image, **kwargs):
        container = await super()._create_container(image, **kwargs)
        limits = self._resource_limits
        if limits is not None:
            try:
                # docker-py 7.x 的 update_container 不支持 nano_cpus
                # （REST API 本身支持）：cpu 用 quota/period 等效表达。
                # memswap 必须与 mem_limit 同时更新（否则 409 Conflict：
                # 已有 memoryswap 不小于新 memory 上限），设为相同值即禁用swap
                container.update(
                    mem_limit=limits.mem_limit,
                    memswap_limit=limits.mem_limit,
                    cpu_quota=int(limits.cpus * 100_000),
                    cpu_period=100_000,
                )
            except Exception as e:  # noqa: BLE001
                # 资源限制失败不阻塞会话创建，退化为daemon级默认限制
                logger.warning(f"为沙箱容器设置资源限额失败（使用daemon默认值）: {e}")
        return container


@dataclass
class _ManagedSandbox:
    session: SandboxSession
    last_used: float = field(default_factory=time.monotonic)


class SandboxManager:
    """聊天会话的沙箱池：惰性创建、空闲TTL/LRU回收、快照恢复"""

    def __init__(self):
        from docker import from_env as docker_from_env

        self._options = DockerSandboxClientOptions(
            image=cfg.sandbox.image,
            labels={"kanade-bot": "chat-sandbox"},
        )
        self._client = KanadeDockerClient(
            docker_from_env(),
            resource_limits=ResourceLimits(
                mem_limit=cfg.sandbox.mem_limit,
                cpus=cfg.sandbox.cpus,
            ),
        )
        self._sandboxes: dict[str, _ManagedSandbox] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: asyncio.Task | None = None

        self._snapshot_dir = cfg.sandbox.snapshot_dir_path
        self._snapshot_dir.mkdir(parents=True, exist_ok=True)

        self._manifest = Manifest(environment=cfg.sandbox.environment)

        driver = get_driver()
        driver.on_startup(self._startup)
        driver.on_shutdown(self._shutdown)

    # ===== 快照 =====

    @staticmethod
    def _safe_name(session_id: str) -> str:
        """会话ID转单一安全路径段"""
        name = session_id.replace("/", "_").replace("\\", "_")
        if name in {"", ".", ".."}:
            raise ValueError(f"会话ID无法转换为安全快照名: {session_id!r}")
        return name

    def _snapshot_for(self, session_id: str) -> LocalSnapshot:
        # 传入SnapshotBase实例时SDK直接使用（resolve_snapshot），
        # 快照文件名为 base_path/<id>.tar，与聊天会话一一对应
        return LocalSnapshot(id=self._safe_name(session_id), base_path=self._snapshot_dir)

    def delete_snapshot(self, session_id: str) -> None:
        """删除会话的快照文件（重置会话时调用）"""
        tar = self._snapshot_dir / f"{self._safe_name(session_id)}.tar"
        tar.unlink(missing_ok=True)

    # ===== 获取与销毁 =====

    async def acquire(self, session_id: str) -> SandboxSession:
        """获取会话对应的沙箱（惰性创建，从快照恢复工作区），刷新使用时间"""
        async with self._lock:
            managed = self._sandboxes.get(session_id)
            if managed is not None and await managed.session.running():
                managed.last_used = time.monotonic()
                return managed.session
            if managed is not None:
                # 会话存在但已停止：丢弃陈旧引用后重建
                self._sandboxes.pop(session_id, None)

            session = await self._client.create(
                manifest=self._manifest,
                options=self._options,
                snapshot=self._snapshot_for(session_id),
            )
            await session.start()
            self._sandboxes[session_id] = _ManagedSandbox(session)
            logger.info(f"已为会话{session_id}创建沙箱（当前共存{len(self._sandboxes)}个）")
            # 超上限时LRU淘汰（不含刚创建的这个）
            await self._evict_over_limit(exclude=session_id)
            return session

    async def destroy(self, session_id: str, *, keep_snapshot: bool = True) -> None:
        """销毁会话沙箱。keep_snapshot=True 时先持久化工作区快照"""
        async with self._lock:
            managed = self._sandboxes.pop(session_id, None)
        if managed is None:
            return
        await self._destroy(managed, keep_snapshot=keep_snapshot)

    async def _destroy(self, managed: _ManagedSandbox, *, keep_snapshot: bool) -> None:
        session = managed.session
        try:
            if keep_snapshot:
                try:
                    await session.stop()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"沙箱快照持久化失败: {e}")
            await session.shutdown()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"销毁沙箱时发生错误: {e}")

    async def destroy_all(self) -> None:
        """销毁全部沙箱（快照保留）"""
        async with self._lock:
            managed_list = list(self._sandboxes.values())
            self._sandboxes.clear()
        for managed in managed_list:
            await self._destroy(managed, keep_snapshot=True)

    # ===== 回收策略 =====

    async def _evict_over_limit(self, *, exclude: str | None = None) -> None:
        """超过最大容器数时LRU淘汰（销毁前快照）"""
        limit = cfg.sandbox.max_concurrent_containers
        candidates = [(sid, m) for sid, m in self._sandboxes.items() if sid != exclude]
        candidates.sort(key=lambda item: item[1].last_used)
        overflow = len(candidates) + (1 if exclude else 0) - limit
        for sid, managed in candidates[: max(overflow, 0)]:
            self._sandboxes.pop(sid, None)
            logger.info(f"沙箱数量超过上限{limit}，LRU销毁会话{sid}的沙箱（快照保留）")
            await self._destroy(managed, keep_snapshot=True)

    async def _sweep_once(self) -> None:
        """回收空闲超时的沙箱（销毁前快照）"""
        now = time.monotonic()
        timeout_sec = cfg.sandbox.idle_timeout_minutes * 60
        expired = [
            (sid, m) for sid, m in self._sandboxes.items() if now - m.last_used >= timeout_sec
        ]
        for sid, managed in expired:
            self._sandboxes.pop(sid, None)
            logger.info(
                f"会话{sid}的沙箱空闲超过{cfg.sandbox.idle_timeout_minutes}分钟，销毁（快照保留）"
            )
            await self._destroy(managed, keep_snapshot=True)

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
        await asyncio.to_thread(self._cleanup_orphan_containers)
        self._sweeper_task = asyncio.get_running_loop().create_task(self._sweeper_loop())

    async def _shutdown(self):
        if self._sweeper_task is not None:
            self._sweeper_task.cancel()
            self._sweeper_task = None
        await self.destroy_all()

    def _cleanup_orphan_containers(self):
        """清理上次进程crash遗留的沙箱容器（按label识别）"""
        try:
            containers = self._client.docker_client.containers.list(
                all=True, filters={"label": "kanade-bot=chat-sandbox"}
            )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"扫描孤儿沙箱容器失败: {e}")
            return
        for container in containers:
            try:
                container.remove(force=True)
                logger.info(f"已清理孤儿沙箱容器: {container.short_id}")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"清理孤儿沙箱容器失败: {container.short_id}: {e}")
