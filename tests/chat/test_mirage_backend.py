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
import os
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
HAVE_UV = shutil.which("uv") is not None


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
        uv_bin = "uv"
        uv_python_dir = None
        venv_python = "3.13"
        venv_timeout = 120

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
    def test_is_official_mirage_backend(self):
        """`KanadeWorkspace` 必须是官方 `MirageWorkspaceBackend` 的真子类（而非重写协议）"""
        import asyncio

        from mirage.agents.pydantic_ai import MirageWorkspaceBackend

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
                self.assertIsInstance(backend, MirageWorkspaceBackend)
                capability = module.SandboxWorkspaceCapability()
                self.assertTrue(hasattr(capability, "get_workspace"))
            finally:
                asyncio.run(ws.close())


# ===== 相对路径绑定与命令行为（需要 sandlock） =====


@pytest.mark.skipif(not HAVE_SANDLOCK, reason="sandlock CLI 未安装")
class OfficialBackendBehaviorTest(unittest.IsolatedAsyncioTestCase):
    """`KanadeWorkspace`（官方 `MirageWorkspaceBackend` + 根绑定）的行为契约"""

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
        await self.backend.write_bytes("rel/a.txt", b"hello")
        landed = self.root / "behavior" / "rel" / "a.txt"
        self.assertTrue(host_exists(landed))
        self.assertEqual(host_read(landed), "hello")

    async def test_relative_read_roundtrip(self):
        await self.backend.write_bytes("b.txt", b"foo bar")
        self.assertEqual(await self.backend.read_bytes("b.txt"), b"foo bar")

    async def test_execute_cwd_is_workspace_root(self):
        """session 初始化后 run 的工作目录即工作区根（相对路径命令可用）"""
        await self.backend.write_bytes("cwdprobe.txt", b"x")
        result = await self.backend.run("pwd && cat cwdprobe.txt", shell=True)
        self.assertEqual(result.exit_code, 0)
        self.assertIn(str(self.root / "behavior"), result.stdout)
        self.assertIn("x", result.stdout)

    async def test_execute_env_injected(self):
        """配置的沙箱环境变量经 session 初始化注入（持久 export，run 克隆继承）"""
        self.module.cfg.sandbox.environment = {"KANADE_PROBE": "42"}  # type: ignore[attr-defined]
        try:
            mgr = _make_manager(self.module, self.root)
            session = await mgr.create("envtest")
            try:
                result = await session.backend.run("echo $KANADE_PROBE", shell=True)
            finally:
                await session.close()
        finally:
            self.module.cfg.sandbox.environment = {}  # type: ignore[attr-defined]
        self.assertEqual(result.stdout.strip(), "42")

    async def test_execute_timeout_cancels(self):
        """官方 run 已内建超时（WorkspaceTimeoutError）"""
        from pydantic_ai.workspaces import WorkspaceTimeoutError

        with self.assertRaises(WorkspaceTimeoutError):
            await self.backend.run("sleep 30", shell=True, timeout=1)

    async def test_cd_does_not_leak_across_runs(self):
        """run 是会话克隆语义：单条命令内的 cd 有效，但不跨命令持久"""
        (self.root / "behavior" / "sub").mkdir(parents=True, exist_ok=True)
        result = await self.backend.run(f"cd {self.session.root}/sub && pwd", shell=True)
        self.assertIn("/sub", result.stdout)
        result = await self.backend.run("pwd", shell=True)
        self.assertNotIn("/sub", result.stdout)

    async def test_absolute_virtual_path_still_works(self):
        """挂载前缀（= 宿主真实路径）开头的完整虚拟路径不受绑定影响"""
        await self.backend.write_bytes(f"{self.session.root}/abs.txt", b"abs")
        self.assertEqual(await self.backend.read_bytes("abs.txt"), b"abs")

    async def test_nonzero_exit_is_a_result(self):
        """非零退出是正常结果，不是异常"""
        result = await self.backend.run("exit 3", shell=True)
        self.assertEqual(result.exit_code, 3)

    async def test_session_write_creates_parents_and_overwrites(self):
        """工具层 `SandboxSession.write`：自动建父目录，且可覆盖"""

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
        await self.backend.write_bytes("doomed.txt", b"x")
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

        self.assertFalse(await self.backend.exists(str(secret)))
        with self.assertRaises(FileNotFoundError):
            await self.backend.read_bytes(str(secret))

    async def test_bot_source_not_readable(self):
        """bot 自己的源码同样读不到"""
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.backend.read_bytes(str(REPO_ROOT / "config.yaml"))

    async def test_workspace_root_is_writable(self):
        """工作区内正常读写，且落到宿主真实目录（免 FUSE，子进程验证）"""
        await self.backend.write_bytes("a/b.txt", b"hello")
        landed = self.root / "chat:iso" / "a" / "b.txt"
        self.assertTrue(host_exists(landed))
        self.assertEqual(host_read(landed), "hello")
        self.assertEqual(await self.backend.read_bytes("a/b.txt"), b"hello")

    async def test_escape_via_dotdot_fails(self):
        """`..` 穿越出工作区根的路径必须失败（VFS 界外不存在）"""
        with self.assertRaises((FileNotFoundError, ValueError)):
            await self.backend.read_bytes("../../etc/passwd")
        self.assertFalse(await self.backend.exists("../../etc/passwd"))


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

    def test_probe_authorizes_system_roots(self):
        """自检探针必须随解释器根一并授权系统根（mirage SYSTEM_READABLE 对齐）

        uv run 把 .venv/bin 前置到 PATH 后，which("python3") 是指向 uv 托管
        解释器的软链，解释器根落在 /home 而非 /usr。只授权解释器根时，
        ld-linux/libc 与 /dev/urandom 被 landlock 拒绝，execvp 以
        EACCES(127) 失败（生产 6.8 内核实测的启动报错）。
        """
        module = self.module
        tmp = Path(self.tmp.name)
        fake_cfg = module.cfg.sandbox
        saved = (
            fake_cfg.landlock_degrade,
            fake_cfg.landlock_real_binary,
            shutil.which,
            subprocess.run,
            os.environ.get("PATH", ""),
        )
        fake_cfg.landlock_degrade = "auto"
        fake_cfg.landlock_real_binary = str(tmp / "real-sandlock")

        # 伪 venv：python3 -> python -> 解释器根不在 /usr 下的真实文件
        fake_bin = tmp / "venv" / "bin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        real_dir = tmp / "uvdir" / "cpython"
        real_dir.mkdir(parents=True, exist_ok=True)
        (real_dir / "python3.13").write_text("")
        (fake_bin / "python").symlink_to(real_dir / "python3.13")
        (fake_bin / "python3").symlink_to("python")
        expected_root = f"/{(real_dir / 'python3.13').resolve().parts[1]}"

        captured: dict[str, list[str]] = {}

        def fake_which(name):
            return str(fake_bin / name) if name == "python3" else saved[2](name)

        def fake_run(argv, *args, **kwargs):
            argv = list(argv)
            if len(argv) > 1 and argv[1] == "check":
                return subprocess.CompletedProcess(
                    argv, 0, stdout="Landlock: ABI v4\nMinimum required: ABI v6", stderr=""
                )
            captured["argv"] = argv
            return subprocess.CompletedProcess(argv, 0, stdout="1\n", stderr="")

        shutil.which = fake_which  # type: ignore[assignment]
        subprocess.run = fake_run  # type: ignore[assignment]
        try:
            abi = module.ensure_landlock(tmp / ".bin")
            self.assertEqual(abi, 4)
        finally:
            fake_cfg.landlock_degrade, fake_cfg.landlock_real_binary = saved[:2]
            shutil.which, subprocess.run = saved[2], saved[3]  # type: ignore[assignment]
            os.environ["PATH"] = saved[4]

        self.assertTrue((tmp / ".bin" / "sandlock").is_file())
        argv = captured["argv"]
        self.assertEqual(argv[0], str(tmp / ".bin" / "sandlock"))
        self.assertEqual(argv[1], "run")
        dash = argv.index("--")
        flags = argv[2:dash]
        r_roots = [flags[i + 1] for i, t in enumerate(flags) if t == "-r"]
        self.assertIn(expected_root, r_roots)
        for d in module.SYSTEM_READABLE_DIRS:
            if Path(d).is_dir():
                self.assertIn(d, r_roots)
        self.assertEqual(len(r_roots), len(set(r_roots)), "授权根不应重复")
        self.assertEqual(argv[dash + 1 :], [str(fake_bin / "python3"), "-c", "print(1)"])

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


# ===== 虚拟环境（宿主 uv 预配，需要 sandlock + uv） =====


@pytest.mark.skipif(not (HAVE_SANDLOCK and HAVE_UV), reason="sandlock/uv CLI 未安装")
class VenvSetupTest(unittest.IsolatedAsyncioTestCase):
    """系统 python3 移除 + 宿主 uv 预配 venv + 装包命令动态注册"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.module = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self):
        self.mgr = _make_manager(self.module, self.root)
        self.mgr._check_uv()  # 解析 uv 与解释器目录（正常由启动钩子执行）
        self.session = await self.mgr.create("venvtest")

    async def asyncTearDown(self):
        await self.session.close()

    async def test_system_python3_unavailable_before_venv(self):
        """venv 未创建时，裸 python3 与显式 /usr/bin/python3 都不可用"""
        for line in ("python3 -V", "/usr/bin/python3 -V"):
            result = await self.session.backend.run(line, shell=True)
            self.assertNotEqual(result.exit_code, 0, line)

    async def test_setup_venv_enables_python3(self):
        """创建 venv 后 python3 可用且指向 venv 解释器"""
        report = await self.session.setup_venv()
        self.assertIn("已创建虚拟环境", report)

        result = await self.session.backend.run(
            "python3 -c 'import sys; print(sys.executable)'", shell=True
        )
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
        self.assertIn(".venv", result.stdout)

    async def test_install_package_import_and_cli(self):
        """装包后可 import，entry point 命令可直接调用，python3 -m 也可用"""
        report = await self.session.setup_venv(packages=["six"])
        self.assertIn("已安装 1 个包", report)

        result = await self.session.backend.run(
            "python3 -c 'import six; print(six.__version__)'", shell=True
        )
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)

        # pytest 提供 pytest/py.test 命令，验证 entry point 动态注册
        report = await self.session.setup_venv(packages=["pytest"])
        self.assertIn("pytest", report)
        result = await self.session.backend.run("pytest --version", shell=True)
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
        result = await self.session.backend.run("python3 -m pytest --version", shell=True)
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)

    async def test_invalid_package_spec_rejected(self):
        """URL/git/本地路径等注入形态被拒绝，且 venv 未被创建"""
        report = await self.session.setup_venv(packages=["requests @ https://evil.example/x.whl"])
        self.assertIn("不合法的包声明", report)
        self.assertFalse((self.session.workspace_dir / ".venv").exists())

    async def test_memory_limit_still_enforced(self):
        """venv 内解释器仍受 sandlock 内存限额约束（256M 配置下 1G 分配失败）"""
        await self.session.setup_venv()
        result = await self.session.backend.run(
            "python3 -c 'b=bytearray(1024*1024*1024); print(\"ok\")'", shell=True
        )
        self.assertNotEqual(result.exit_code, 0)


# ===== 原生 venv 回退（uv 不可用时，需要 sandlock） =====


@pytest.mark.skipif(
    not (HAVE_SANDLOCK and shutil.which("python3")), reason="sandlock/python3 未安装"
)
class NativeVenvFallbackTest(unittest.IsolatedAsyncioTestCase):
    """uv 缺失时回退 `python3 -m venv` + venv 内 pip"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.module = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    async def asyncSetUp(self):
        self.mgr = _make_manager(self.module, self.root)
        # 模拟 uv 完全不可用（走 _startup 的回退解析：优选 python{venv_python}）
        original = self.module.cfg.sandbox.uv_bin
        self.module.cfg.sandbox.uv_bin = "uv-definitely-not-exist-xyz"
        try:
            await self.mgr._startup()
        finally:
            self.module.cfg.sandbox.uv_bin = original  # type: ignore[attr-defined]
        assert self.mgr._uv_bin is None
        self.session = await self.mgr.create("native")

    async def asyncTearDown(self):
        await self.session.close()

    async def test_native_venv_enables_python3(self):
        """python -m venv 创建后 python3 可用且指向 venv 解释器"""
        report = await self.session.setup_venv()
        self.assertIn("python -m venv", report)
        self.assertIn("已创建虚拟环境", report)

        result = await self.session.backend.run(
            "python3 -c 'import sys; print(sys.executable)'", shell=True
        )
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
        self.assertIn(".venv", result.stdout)

    async def test_native_venv_install_package(self):
        """原生方式用 venv 内 pip 装包（python3 -m venv 默认自带 pip）"""
        report = await self.session.setup_venv(packages=["six"])
        self.assertIn("已安装 1 个包", report)

        result = await self.session.backend.run(
            "python3 -c 'import six; print(six.__version__)'", shell=True
        )
        self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)

    async def test_unknown_version_falls_back_to_default(self):
        """python_version 在宿主找不到对应解释器时用默认 python3 并注明"""
        report = await self.session.setup_venv(python_version="3.99")
        self.assertIn("未找到宿主 python3.99", report)
        self.assertTrue((self.session.workspace_dir / ".venv").exists())


@pytest.mark.skipif(not HAVE_SANDLOCK, reason="sandlock CLI 未安装")
class UvOptionalStartupTest(unittest.IsolatedAsyncioTestCase):
    """uv 不可用不再阻断沙箱启动（Python 环境回退原生方式）"""

    async def test_startup_survives_missing_uv(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        module = _load_sandbox_module(Path(tmp.name).resolve())

        original_uv_bin = module.cfg.sandbox.uv_bin
        module.cfg.sandbox.uv_bin = "uv-definitely-not-exist-xyz"  # type: ignore[attr-defined]
        try:
            mgr = _make_manager(module, Path(tmp.name).resolve())
            await mgr._startup()  # 不应抛出
            self.assertIsNone(mgr._uv_bin)
            # 回退解释器若在系统目录外，其安装根会被补进只读授权
            self.assertIsNotNone(mgr._host_python)
            if mgr._uv_python_dir is not None:
                self.assertTrue(mgr._uv_python_dir.is_dir())
        finally:
            module.cfg.sandbox.uv_bin = original_uv_bin  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
