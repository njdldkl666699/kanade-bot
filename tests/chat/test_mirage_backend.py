"""mirage 官方 Pydantic AI 集成（`KanadeWorkspace`）的行为测试。

mirage 提供 `PydanticAIWorkspace`（`pydantic-ai-backend` 的 `SandboxProtocol`
实现），本项目在其上加**工作区根绑定**（免 FUSE 布局下相对路径会落到 VFS
根 `/`）与 `execute` 超时。这里验证官方 backend 的行为契约 + 本项目特有的
**隔离边界**（模型读不到 bot 的配置/密钥/源码）。

**宿主落地断言必须走子进程**：mirage 会给整个进程打 `os`/`open` 补丁
（`mirage.runtime.python.host.*`），本布局挂载前缀 = 宿主真实路径，进程内
`Path(...).is_file()`/`read_text()` 命中挂载前缀即被路由回 VFS——查到的
是 VFS 视图（含索引缓存竞态）而非宿主真身。子进程没有补丁，看到的才是
宿主真实状态（写入本身始终正确落盘，已用子进程验证）。

`sandbox.py` 依赖 nonebot 插件配置（`from ..config import cfg`），直接导入会
触发整个插件链（`require("model_updater")`）。这里用假包链把它单独加载。
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SANDBOX_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/sandbox.py"

HAVE_SANDLOCK = shutil.which("sandlock") is not None


def host_exists(path: Path) -> bool:
    """子进程检查宿主路径，绕过 mirage 的进程级 os 补丁"""
    return subprocess.run(["test", "-e", str(path)], check=False).returncode == 0


def host_read(path: Path) -> str | None:
    """子进程读取宿主文件内容，绕过 mirage 的进程级 os 补丁"""
    r = subprocess.run(["cat", str(path)], capture_output=True, text=True, check=False)
    return r.stdout if r.returncode == 0 else None


def _load_sandbox_module(workspace_root: Path | None = None):
    """把 sandbox.py 加载成 kchat.agent.sandbox，注入可控的 cfg"""
    for name in ("kchat", "kchat.agent"):
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod

    class FakeSandboxConfig:
        enabled = True
        memory_limit = "256M"
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


# ===== 官方 backend 身份 =====


class BackendIdentityTest(unittest.TestCase):
    def test_is_official_pydantic_ai_workspace(self):
        """`KanadeWorkspace` 必须是官方 backend 的真子类（而非重写协议）"""
        import asyncio

        from mirage.agents.pydantic_ai import PydanticAIWorkspace
        from pydantic_ai_backends.protocol import SandboxProtocol

        module = _load_sandbox_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            ws = module.Workspace(
                {str(root): (module.DiskVFS(str(root)), module.MountMode.EXEC)},
                mode=module.MountMode.EXEC,
            )
            ws.create_session("test")
            try:
                backend = module.KanadeWorkspace(workspace=ws, root=str(root), session_id="test")
                self.assertIsInstance(backend, PydanticAIWorkspace)
                self.assertIsInstance(backend, SandboxProtocol)
            finally:
                asyncio.run(ws.close())


# ===== 相对路径绑定与命令行为（需要 sandlock） =====


@pytest.mark.skipif(not HAVE_SANDLOCK, reason="sandlock CLI 未安装")
class OfficialBackendBehaviorTest(unittest.IsolatedAsyncioTestCase):
    """`KanadeWorkspace`（官方 `PydanticAIWorkspace` + 根绑定）的行为契约"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.module = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self):
        self.mgr = _make_manager(self.module, self.root)
        self.session = await self.mgr.create("behavior")
        self.backend = self.session.backend

    async def asyncTearDown(self):
        await self.session.close()

    async def test_relative_write_lands_in_host_workspace(self):
        """相对路径写入必须落到宿主工作区（官方裸实现会静默写进 VFS 根 overlay）"""
        result = await self.backend.awrite("rel/a.txt", "hello")
        self.assertIsNone(result.error)
        landed = self.root / "behavior" / "rel" / "a.txt"
        self.assertTrue(host_exists(landed))
        self.assertEqual(host_read(landed), "hello")

    async def test_relative_read_and_edit(self):
        await self.backend.awrite("b.txt", "foo bar")
        self.assertIn("1\tfoo bar", await self.backend.aread("b.txt"))
        edited = await self.backend.aedit("b.txt", "foo", "baz")
        self.assertIsNone(edited.error)
        self.assertEqual(host_read(self.root / "behavior" / "b.txt"), "baz bar")

    async def test_execute_cwd_is_workspace_root(self):
        """session 初始化后 execute 的工作目录即工作区根（相对路径命令可用）"""
        await self.backend.awrite("cwdprobe.txt", "x")
        result = await self.backend.aexecute("pwd && cat cwdprobe.txt")
        self.assertEqual(result.exit_code, 0)
        self.assertIn(str(self.root / "behavior"), result.output)
        self.assertIn("x", result.output)

    async def test_execute_env_injected(self):
        """配置的沙箱环境变量经 session 初始化注入（持久 export）"""
        self.module.cfg.sandbox.environment = {"KANADE_PROBE": "42"}  # type: ignore[attr-defined]
        try:
            mgr = _make_manager(self.module, self.root)
            session = await mgr.create("envtest")
            try:
                result = await session.backend.aexecute("echo $KANADE_PROBE")
            finally:
                await session.close()
        finally:
            self.module.cfg.sandbox.environment = {}  # type: ignore[attr-defined]
        self.assertEqual(result.output.strip(), "42")

    async def test_execute_timeout_cancels(self):
        """官方实现忽略 timeout；子类必须真正超时中断"""
        with self.assertRaises(TimeoutError):
            await self.backend.aexecute("sleep 30", timeout=1)

    async def test_grep_defaults_to_workspace_root(self):
        await self.backend.awrite("grepdir/needle.txt", "needle here\nother line\n")
        matches = await self.backend.agrep_raw("needle")
        # GrepMatch 是 TypedDict（运行时即 dict）
        self.assertTrue(any("needle here" in m["line"] for m in matches))

    async def test_glob_defaults_to_workspace_root(self):
        await self.backend.awrite("globdir/target.bin", "x")
        infos = await self.backend.aglob_info("**/target.bin")
        self.assertTrue(any(i["name"] == "target.bin" for i in infos))

    async def test_absolute_virtual_path_still_works(self):
        """挂载前缀（= 宿主真实路径）开头的完整虚拟路径不受绑定影响"""
        result = await self.backend.awrite(f"{self.session.root}/abs.txt", "abs")
        self.assertIsNone(result.error)
        self.assertEqual(await self.backend.aread_bytes("abs.txt"), b"abs")

    async def test_nonzero_exit_is_a_result(self):
        """非零退出是正常结果，不是异常"""
        result = await self.backend.aexecute("exit 3")
        self.assertEqual(result.exit_code, 3)

    async def test_session_write_creates_parents_and_overwrites(self):
        """工具层 `SandboxSession.write`：自动建父目录，且可覆盖（官方 awrite 拒绝覆盖）"""

        def blob(data: bytes):
            return types.SimpleNamespace(read=lambda: data)

        target = self.root / "behavior" / "deep" / "nest" / "file.png"
        await self.session.write(Path("deep/nest/file.png"), blob(b"1"))
        await self.session.write(Path("deep/nest/file.png"), blob(b"2"))
        self.assertEqual(host_read(target), "2")
        stream = await self.session.read(Path("deep/nest/file.png"))
        self.assertEqual(stream.read(), b"2")

    async def test_delete_workspace_removes_host_dir(self):
        """挂载存活时 delete_workspace（rmtree）也必须真删宿主目录"""
        await self.backend.awrite("doomed.txt", "x")
        ws_dir = self.session.root
        self.assertTrue(host_exists(Path(ws_dir) / "doomed.txt"))
        self.mgr.delete_workspace("behavior")
        self.assertFalse(host_exists(Path(ws_dir)))


# ===== 本项目特有的隔离边界 =====


@pytest.mark.skipif(not HAVE_SANDLOCK, reason="sandlock CLI 未安装")
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
        self.session = await self.mgr.create("chat:iso")
        self.backend = self.session.backend

    async def asyncTearDown(self):
        await self.session.close()

    async def test_workspace_outside_paths_do_not_exist(self):
        """工作区外的路径在 VFS 里根本不存在"""
        secret = self.root / "config.yaml"
        secret.write_text("api_key: sk-should-never-be-visible\n")

        self.assertFalse(await self.backend.aexists(str(secret)))
        with self.assertRaises(FileNotFoundError):
            await self.backend.aread_bytes(str(secret))

    async def test_bot_source_not_readable(self):
        """bot 自己的源码同样读不到"""
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.backend.aread_bytes(str(REPO_ROOT / "config.yaml"))

    async def test_workspace_root_is_writable(self):
        """工作区内正常读写，且落到宿主真实目录（免 FUSE，子进程验证）"""
        result = await self.backend.awrite("a/b.txt", "hello")
        self.assertIsNone(result.error)
        landed = self.root / "chat:iso" / "a" / "b.txt"
        self.assertTrue(host_exists(landed))
        self.assertEqual(host_read(landed), "hello")
        self.assertEqual(await self.backend.aread_bytes("a/b.txt"), b"hello")

    async def test_escape_via_dotdot_fails(self):
        """`..` 穿越出工作区根的路径必须失败（VFS 界外不存在）"""
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.backend.aread_bytes("../../etc/passwd")
        self.assertFalse(await self.backend.aexists("../../etc/passwd"))


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
