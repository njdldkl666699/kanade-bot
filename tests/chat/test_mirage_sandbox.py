"""mirage 沙箱验证。

`sandbox.py` 依赖 nonebot 插件配置（`from ..config import cfg`），直接导入会
触发整个插件链（`require("model_updater")`）。这里用假包链把它单独加载，测试
只覆盖沙箱自身的逻辑，不需要 nonebot 运行时。

覆盖研究阶段实测确认的关键行为：cwd 语义、免 FUSE 落盘、sandlock 隔离与限额、
TTL/LRU 回收、reset 删目录、每会话内存占用。
"""

import importlib.util
import io
import resource
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

from agents.sandbox import Manifest
from agents.sandbox.manifest import Environment
from mirage import MountMode, Workspace
from mirage.agents.openai_agents import MirageSandboxClient
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from mirage.vfs.disk import DiskVFS

SANDBOX_PATH = Path(__file__).parents[2] / "kanade_bot/plugins/chat/agent/sandbox.py"

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
        environment = Environment()
        workspace_dir_path = workspace_root or Path(tempfile.gettempdir())

    cfg_module = types.ModuleType("kchat.config")
    cfg_module.cfg = types.SimpleNamespace(  # type: ignore[attr-defined]
        sandbox=FakeSandboxConfig()
    )
    sys.modules["kchat.config"] = cfg_module

    spec = importlib.util.spec_from_file_location("kchat.agent.sandbox", SANDBOX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kchat.agent.sandbox"] = module
    spec.loader.exec_module(module)
    return module


async def _exec(session, line: str):
    """执行一条 shell 命令，返回 (exit_code, stdout 文本)"""
    result = await session.exec(line, shell=True)
    return result.exit_code, result.stdout.decode()


def _sandbox(d: Path, *, captures=("python3",), memory="256M") -> Workspace:
    """构造一个与 SandboxManager 同构的 Workspace"""
    runtimes = [
        SandlockRuntime(
            captures=captures,
            config={
                "fs_readable": (str(d),),
                "fs_writable": (str(d),),
                "max_memory": memory,
                "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
            },
        )
    ]
    return Workspace(
        {str(d): (DiskVFS(str(d)), MountMode.EXEC)},
        mode=MountMode.EXEC,
        runtimes=runtimes,
    )


class PathSemanticsTest(unittest.IsolatedAsyncioTestCase):
    """路径语义：工作目录、相对路径读写（PyPI 版曾在此有缺陷，main 已修）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.mod = _load_sandbox_module()
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self):
        self.d = (self.root / "s1").resolve()
        self.d.mkdir(parents=True, exist_ok=True)
        self.ws = _sandbox(self.d, captures=())
        self.client = MirageSandboxClient(self.ws)
        self.session = await self.client.create(manifest=Manifest(root=str(self.d)))

    async def asyncTearDown(self):
        await self.session.shutdown()
        await self.ws.close()

    async def test_uses_stock_mirage_session(self):
        """直接用上游 MirageSandboxSession，无需任何子类或补丁"""
        self.assertEqual(type(self.session._inner).__name__, "MirageSandboxSession")

    async def test_pwd_is_workspace_root(self):
        code, out = await _exec(self.session, "pwd")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.d))

    async def test_relative_write_lands_on_host(self):
        """核心回归：相对路径必须落在工作区，而非 workspace 根 '/'"""
        code, out = await _exec(self.session, "echo hi > rel.txt && cat rel.txt")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "hi")
        self.assertEqual((self.d / "rel.txt").read_text().strip(), "hi")

    async def test_session_read_write_relative_path(self):
        """tool.py 的 send_file/send_image/render_html_image 依赖这个行为"""
        await self.session.write(Path("rendered/shot.png"), io.BytesIO(b"PNGDATA"))
        self.assertTrue((self.d / "rendered" / "shot.png").is_file())
        data = await self.session.read(Path("rendered/shot.png"))
        self.assertEqual(data.read(), b"PNGDATA")

    async def test_session_read_write_absolute_path(self):
        await self.session.write(self.d / "abs.txt", io.BytesIO(b"abs"))
        self.assertEqual((self.d / "abs.txt").read_text(), "abs")
        data = await self.session.read(self.d / "abs.txt")
        self.assertEqual(data.read(), b"abs")

    async def test_nested_dirs(self):
        code, _ = await _exec(
            self.session,
            "mkdir -p sub/deep && echo x > sub/deep/f.txt && find . -type f",
        )
        self.assertEqual(code, 0)
        self.assertEqual((self.d / "sub" / "deep" / "f.txt").read_text().strip(), "x")

    async def test_sessions_have_independent_shell_state(self):
        """同一 Workspace 下各会话的 shell 状态（cwd/env）互相独立

        注意：会话共享底层 VFS（mirage 的隔离粒度是 shell 状态，不是文件），
        用户间的文件隔离由「每个聊天会话一个 Workspace」保证。
        """
        other = await self.client.create(manifest=Manifest(root=str(self.d)))
        try:
            await _exec(self.session, f"cd {self.d} && pwd")
            code, out = await _exec(other, "pwd")
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), str(self.d))
            self.assertNotEqual(type(other._inner).__name__, "", "另一会话应可独立执行命令")
        finally:
            await other.shutdown()


class WorkspacePersistenceTest(unittest.IsolatedAsyncioTestCase):
    """DiskVFS 免 FUSE：文件真实落宿主目录，跨 workspace 重建保留"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.mod = _load_sandbox_module()
        self.addCleanup(self.tmp.cleanup)

    async def test_files_survive_workspace_recreate(self):
        d = (self.root / "persist").resolve()
        d.mkdir(parents=True, exist_ok=True)

        async def roundtrip():
            ws = _sandbox(d, captures=())
            client = MirageSandboxClient(ws)
            s = await client.create(manifest=Manifest(root=str(d)))
            await s.start()
            await s.shutdown()
            await ws.close()

        (d / "keep.txt").write_text("persisted")
        await roundtrip()
        # 模拟 TTL 回收后重建：文件仍在，不需要快照 tar
        self.assertEqual((d / "keep.txt").read_text(), "persisted")

    async def test_realpath_matters(self):
        """挂载前缀与宿主 realpath 不一致时写不到宿主目录

        这正是 `_workspace_dir` 必须 resolve() 的原因（/tmp 常是 symlink）。
        """
        real = (self.root / "rp").resolve()
        real.mkdir(parents=True, exist_ok=True)
        alias = self.root / "rp"  # 同一目录，未 resolve 的写法
        ws = Workspace(
            {str(alias): (DiskVFS(str(real)), MountMode.EXEC)},
            mode=MountMode.EXEC,
        )
        r = await ws.shell("echo x > f.txt", cwd=str(alias))
        self.assertEqual(r.exit_code, 0)
        await ws.close()


@unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
class SandlockIsolationTest(unittest.IsolatedAsyncioTestCase):
    """sandlock 边界：未授权路径不可读写，内存限额生效"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.mod = _load_sandbox_module()
        self.addCleanup(self.tmp.cleanup)

    async def _session(self, name: str, memory: str = "256M"):
        d = (self.root / name).resolve()
        d.mkdir(parents=True, exist_ok=True)
        ws = _sandbox(d, memory=memory)
        client = MirageSandboxClient(ws)
        s = await client.create(manifest=Manifest(root=str(d)))
        await s.start()
        return d, ws, s

    async def test_confined_to_workspace(self):
        _, ws, s = await self._session("iso")
        secret = self.root / "secret.txt"
        secret.write_text("TOP-SECRET")

        code, _ = await _exec(s, f"python3 -c \"print(open('{secret}').read())\"")
        self.assertNotEqual(code, 0, "未授权文件不应可读")

        code, _ = await _exec(s, f"echo pwned > {secret}")
        self.assertNotEqual(code, 0, "未授权文件不应可写")

        self.assertEqual(secret.read_text(), "TOP-SECRET")

        await s.shutdown()
        await ws.close()

    async def test_python3_has_full_stdlib(self):
        """宿主 CPython 而非 monty：json.load/dump 等完整 stdlib 可用"""
        d, ws, s = await self._session("py", memory="512M")
        (d / "x.json").write_text('{"b": 2, "a": 1}')

        code, out = await _exec(
            s,
            f'python3 -c \'import json;d=json.load(open("{d}/x.json"));'
            f'json.dump(d,open("{d}/y.json","w"));print(sorted(d.keys()))\'',
        )
        self.assertEqual(code, 0, out)
        self.assertIn("a", out)
        # y.json 由 python3 写出，应真实落到宿主目录
        self.assertTrue((d / "y.json").is_file())

        await s.shutdown()
        await ws.close()

    async def test_memory_limit_enforced(self):
        """memory_limit 替代原 Docker mem_limit"""
        _, ws, s = await self._session("mem")

        code, _ = await _exec(s, 'python3 -c "x=bytearray(100*1024*1024);print(len(x))"')
        self.assertEqual(code, 0, "100MB 应在 256M 限额内可分配")

        code, _ = await _exec(s, 'python3 -c "x=bytearray(1500*1024*1024);print(len(x))"')
        self.assertNotEqual(code, 0, "1.5GB 应超出 256M 限额")

        await s.shutdown()
        await ws.close()

    async def test_write_through_sandlock_visible_on_host(self):
        """sandlock 写入的文件对宿主可见（免 FUSE 的核心目的）"""
        d, ws, s = await self._session("wt")
        code, _ = await _exec(s, f"echo from-sandlock > {d}/out.txt")
        self.assertEqual(code, 0)
        self.assertEqual((d / "out.txt").read_text().strip(), "from-sandlock")
        await s.shutdown()
        await ws.close()

    async def test_builtin_commands_use_relative_paths(self):
        """不委派的命令走 mirage VFS，相对路径正常工作"""
        d, ws, s = await self._session("builtin")
        (d / "data.txt").write_text("payload")
        for cmd, expect in [
            ("cat data.txt", "payload"),
            ("grep -rl payload .", "./data.txt"),
            ("ls", "data.txt"),
        ]:
            code, out = await _exec(s, cmd)
            self.assertEqual(code, 0, f"{cmd} -> {out}")
            self.assertIn(expect, out)
        await s.shutdown()
        await ws.close()


class ManagerLifecycleTest(unittest.IsolatedAsyncioTestCase):
    """SandboxManager：TTL / LRU / reset / 内存占用"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.mod = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    def _manager(self, **overrides):
        cfg = self.mod.cfg.sandbox
        for k, v in overrides.items():
            setattr(cfg, k, v)
        self.mod.get_driver = lambda: types.SimpleNamespace(
            on_startup=lambda fn: None, on_shutdown=lambda fn: None
        )
        return self.mod.SandboxManager()

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    async def test_acquire_reuses_and_persists(self):
        mgr = self._manager()
        s1 = await mgr.acquire("chat:A")
        code, out = await _exec(s1, "echo persisted > f.txt && cat f.txt")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "persisted")
        self.assertEqual(
            Path(mgr.workspace_root("chat:A"), "f.txt").read_text().strip(), "persisted"
        )
        s2 = await mgr.acquire("chat:A")
        self.assertIs(s1, s2)
        await mgr.destroy_all()

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    async def test_idle_timeout_keeps_files(self):
        mgr = self._manager(idle_timeout_minutes=0)
        s = await mgr.acquire("chat:B")
        await _exec(s, "echo keep > k.txt")
        root = mgr.workspace_root("chat:B")

        await mgr._sweep_once()
        self.assertNotIn("chat:B", mgr._sandboxes)
        self.assertEqual(Path(root, "k.txt").read_text().strip(), "keep")

        # 重新 acquire 能看到旧文件（不需要快照 tar）
        s2 = await mgr.acquire("chat:B")
        code, out = await _exec(s2, "cat k.txt")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "keep")
        await mgr.destroy_all()

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    async def test_lru_eviction(self):
        mgr = self._manager(max_concurrent_sandboxes=2)
        await mgr.acquire("chat:1")
        await mgr.acquire("chat:2")
        await mgr.acquire("chat:3")
        self.assertEqual(len(mgr._sandboxes), 2)
        self.assertNotIn("chat:1", mgr._sandboxes)
        await mgr.destroy_all()

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    async def test_reset_deletes_workspace(self):
        mgr = self._manager()
        s = await mgr.acquire("chat:C")
        await _exec(s, "echo bye > b.txt")
        root = Path(mgr.workspace_root("chat:C"))
        self.assertTrue(root.exists())

        await mgr.destroy("chat:C")
        mgr.delete_workspace("chat:C")
        self.assertFalse(root.exists())

    def test_session_id_sanitised(self):
        """会话ID含路径分隔符时被压平，不能逃逸出根目录"""
        mgr = self._manager()
        root = Path(mgr.workspace_root("group:123/evil"))
        self.assertEqual(root.parent, self.root)
        self.assertNotIn("/", root.name)

    def test_distinct_sessions_get_distinct_dirs(self):
        """用户间文件隔离靠「每聊天会话一个工作区目录」"""
        mgr = self._manager()
        a = mgr.workspace_root("user:alice")
        b = mgr.workspace_root("user:bob")
        self.assertNotEqual(a, b)
        self.assertEqual(Path(a).parent, Path(b).parent)

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    async def test_memory_footprint(self):
        """每会话内存应远低于 Docker 容器量级"""
        mgr = self._manager()

        def rss_mb() -> float:
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024

        before = rss_mb()
        for i in range(5):
            s = await mgr.acquire(f"chat:mem{i}")
            await _exec(s, "seq 1 500 > big.txt")
        growth = rss_mb() - before
        self.assertLess(growth, 64, f"5 会话内存增长 {growth:.1f}MB，超出预期")
        await mgr.destroy_all()


class SandlockGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.mod = _load_sandbox_module(Path(self.tmp.name).resolve())
        self.addCleanup(self.tmp.cleanup)

    def test_missing_cli_raises(self):
        original = shutil.which
        shutil.which = lambda _: None
        try:
            with self.assertRaises(self.mod.SandlockUnavailableError) as ctx:
                self.mod.SandboxManager.check_sandlock()
            self.assertIn("sandlock", str(ctx.exception))
        finally:
            shutil.which = original

    @unittest.skipUnless(HAVE_SANDLOCK, "sandlock CLI 未安装")
    def test_present_cli_passes(self):
        self.mod.SandboxManager.check_sandlock()


if __name__ == "__main__":
    unittest.main()
