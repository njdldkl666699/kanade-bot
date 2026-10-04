"""`MirageBackend` 的 Pydantic AI 协议一致性测试。

用官方 `WorkspaceBackendSuite` 验证自定义后端符合框架契约；另加本项目
特有的**隔离边界**测试（模型读不到 bot 的配置/密钥/源码）。

`sandbox.py` 依赖 nonebot 插件配置（`from ..config import cfg`），直接导入会
触发整个插件链（`require("model_updater")`）。这里用假包链把它单独加载。
"""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

import pytest
from pydantic_ai.workspaces.conformance import WorkspaceBackendSuite

REPO_ROOT = Path(__file__).parents[2]
SANDBOX_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/sandbox.py"

HAVE_SANDLOCK = shutil.which("sandlock") is not None


def _load_sandbox_module(workspace_root: Path | None = None):
    """把 sandbox.py 加载成 kchat.agent.sandbox，注入可控的 cfg"""
    for name in ("kchat", "kchat.agent"):
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod

    class FakeSandboxConfig:
        enabled = True
        memory_limit = "256M"
        max_concurrent_sandboxes = 4
        idle_timeout_minutes = 30
        sweeper_interval_minutes = 5
        environment: dict[str, str] = {}
        workspace_dir_path = workspace_root or Path(tempfile.gettempdir())
        landlock_degrade = "auto"
        landlock_real_binary = None

    cfg_module = types.ModuleType("kchat.config")
    cfg_module.cfg = types.SimpleNamespace(sandbox=FakeSandboxConfig())  # type: ignore[attr-defined]
    sys.modules["kchat.config"] = cfg_module

    spec = importlib.util.spec_from_file_location("kchat.agent.sandbox", SANDBOX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kchat.agent.sandbox"] = module
    spec.loader.exec_module(module)
    return module


def _make_manager(module, root: Path):
    """构造一个不注册 nonebot 生命周期钩子的 SandboxManager"""
    module.get_driver = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        on_startup=lambda fn: None, on_shutdown=lambda fn: None
    )
    return module.SandboxManager()


# ===== 官方协议一致性 =====


@pytest.mark.skipif(not HAVE_SANDLOCK, reason="sandlock CLI 未安装")
class TestMirageBackendConformance(WorkspaceBackendSuite):
    """官方 `WorkspaceBackendSuite`：逐条验证后端契约"""

    @pytest.fixture
    def anyio_backend(self):
        return "asyncio"

    @pytest.fixture
    async def backend(self):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name).resolve()
        module = _load_sandbox_module(root)
        mgr = _make_manager(module, root)
        session = await mgr.acquire("conformance")
        try:
            yield session.backend
        finally:
            await mgr.destroy_all()
            tmp.cleanup()

    @pytest.fixture
    def has_real_posix_shell(self) -> bool:
        # mirage 内置 shell 不是真的 POSIX 进程，部分字节级规则不适用
        return True

    @pytest.fixture
    def filesystem_honors_shell_permissions(self) -> bool:
        """mirage VFS 不做 POSIX 权限检查（隔离靠 Landlock 与 VFS 可见性）"""
        return False

    @pytest.fixture
    def enforces_parent_file_errors(self) -> bool:
        """mirage VFS 有真实路径遍历，会区分「父路径是文件」"""
        return True


# ===== 协议符合性（不需要 sandlock） =====


class ProtocolConformanceTest(unittest.IsolatedAsyncioTestCase):
    """`MirageBackend` 结构上满足 Pydantic AI 的三个可选协议"""

    async def test_protocol_membership(self):
        from mirage import MountMode, Workspace
        from mirage.vfs.disk import DiskVFS
        from pydantic_ai.workspaces import (
            SupportsCommands,
            SupportsFilesystem,
            SupportsRealpath,
            WorkspaceBackend,
        )

        module = _load_sandbox_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            ws = Workspace(
                {str(root): (DiskVFS(str(root)), MountMode.EXEC)},
                mode=MountMode.EXEC,
            )
            ws.create_session("test")
            backend = module.MirageBackend(workspace=ws, root=str(root), session_id="test")
            self.assertIsInstance(backend, WorkspaceBackend)
            self.assertIsInstance(backend, SupportsCommands)
            self.assertIsInstance(backend, SupportsFilesystem)
            self.assertIsInstance(backend, SupportsRealpath)
            await ws.close()


# ===== 本项目特有的隔离边界 =====


class IsolationBoundaryTest(unittest.IsolatedAsyncioTestCase):
    """**回归重点**：模型读不到 bot 的配置 / 密钥 / 源码

    这是选 mirage 而非 bubblewrap 的核心原因（bwrap 用 `--ro-bind / /`，
    沙箱内 `cat` 能直接读出 bot 的 `config.yaml`）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.module = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self):
        self.mgr = _make_manager(self.module, self.root)
        self.session = await self.mgr.acquire("chat:iso")
        self.backend = self.session.backend

    async def asyncTearDown(self):
        await self.mgr.destroy_all()

    async def test_workspace_outside_paths_do_not_exist(self):
        """工作区外的路径在 VFS 里根本不存在"""
        secret = self.root / "config.yaml"
        secret.write_text("api_key: sk-should-never-be-visible\n")

        self.assertFalse(await self.backend.exists(str(secret)))
        with self.assertRaises(FileNotFoundError):
            await self.backend.read_bytes(str(secret))

    async def test_bot_source_not_readable(self):
        """bot 自己的源码同样读不到"""
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.backend.read_bytes(str(REPO_ROOT / "config.yaml"))

    async def test_workspace_root_is_writable(self):
        """工作区内正常读写，且落到宿主真实目录（免 FUSE）"""
        await self.backend.write_bytes("a/b.txt", b"hello")
        self.assertEqual(await self.backend.read_bytes("a/b.txt"), b"hello")
        landed = Path(await self.backend.working_dir()) / "a/b.txt"
        self.assertTrue(landed.is_file())
        self.assertEqual(landed.read_bytes(), b"hello")

    async def test_relative_paths_resolve_to_workspace_root(self):
        """相对路径必须落在工作区，而不是虚拟根 '/'"""
        result = await self.backend.run("pwd", shell=True)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.stdout.strip(), await self.backend.working_dir())

    async def test_remove_refuses_workspace_root(self):
        with self.assertRaises(ValueError):
            await self.backend.remove(await self.backend.working_dir())

    async def test_command_nonzero_exit_is_a_result(self):
        """非零退出是正常结果，不是异常"""
        result = await self.backend.run("exit 3", shell=True)
        self.assertEqual(result.exit_code, 3)

    async def test_timeout_raises_workspace_timeout(self):
        from pydantic_ai.workspaces import WorkspaceTimeoutError

        with self.assertRaises(WorkspaceTimeoutError):
            await self.backend.run("sleep 5", shell=True, timeout=0.5)


class LandlockDegradeTest(unittest.TestCase):
    """Landlock ABI 降级决策（生产内核 6.8 = ABI v4 < v6）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.module = _load_sandbox_module(Path(self.tmp.name).resolve())
        self.addCleanup(self.tmp.cleanup)

    def test_parse_abi(self):
        self.assertEqual(self.module._parse_abi("Landlock: ABI v4\nMin: ABI v6"), 4)
        self.assertEqual(self.module._parse_abi("Landlock: ABI v12"), 12)
        self.assertIsNone(self.module._parse_abi("no abi here"))

    def test_wrapper_uses_posix_sh_not_bash(self):
        """wrapper 必须是 /bin/sh：`${@:2}` 是 bash 扩展，dash 会静默失效"""
        wrapper = self.module.LANDLOCK_WRAPPER
        self.assertTrue(wrapper.startswith("#!/bin/sh"))
        self.assertNotIn("${@:2}", wrapper)
        self.assertIn('sub="$1"; shift', wrapper)

    def test_wrapper_injects_before_double_dash(self):
        """降级参数必须插在 `"$@"` 之前，否则会被当成被沙箱命令的参数"""
        wrapper = self.module.LANDLOCK_WRAPPER
        # 用 rindex：注释里也出现过 "$@"，要看 exec 那一行真正的位置
        self.assertLess(wrapper.rindex("--allow-degraded"), wrapper.rindex('"$@"'))
        self.assertIn("--", wrapper, "wrapper 必须保留 sandlock 的 -- 分隔符")

    def test_missing_cli_raises(self):
        original_which = shutil.which
        original_cfg = self.module.cfg.sandbox.landlock_real_binary
        shutil.which = lambda _: None  # type: ignore[assignment]
        self.module.cfg.sandbox.landlock_real_binary = None
        try:
            with self.assertRaises(self.module.SandlockUnavailableError) as ctx:
                self.module.check_sandlock()
            self.assertIn("sandlock", str(ctx.exception))
        finally:
            shutil.which = original_which  # type: ignore[assignment]
            self.module.cfg.sandbox.landlock_real_binary = original_cfg

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    def test_real_sandlock_present(self):
        abi = self.module.check_sandlock()
        self.assertGreaterEqual(abi, self.module.LANDLOCK_MIN_ABI)


if __name__ == "__main__":
    unittest.main()
