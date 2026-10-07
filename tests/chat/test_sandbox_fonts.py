"""沙箱字体授权、图片暂存与 HOME 缓存环境的测试。

生产问题驱动的三项修复：

1. **字体**：sandlock 受限进程默认只读工作区与 uv 解释器目录，且以
   `--clean-env` 启动（无 `HOME`），导致 matplotlib/PIL 缺中文字体、
   字体缓存无落点。`sandbox_runtime_config` 统一授权字体目录并注入
   `HOME`/`XDG_*`。
2. **图片暂存**：用户发送/引用的图片缓存在宿主 `cache/auto_clear/`，
   沙箱工作区读不到；`_stage_images_to_sandbox` 把它们写入工作区
   `images/`，工具才能按相对路径取用。
3. （需要 sandlock + uv 的端到端用例验证真实受限进程内的字体可读性
   与缓存可写性。）

`sandbox.py`/`manager.py` 依赖 nonebot 插件配置（模块级
`get_plugin_config`/`get_driver`），直接导入会触发整个插件链，这里用
假包链单独加载（与 `test_mirage_backend.py` 同一套路）。
"""

from __future__ import annotations

import base64
import importlib.util
import shutil
import sys
import tempfile
import types
import unittest
from io import BytesIO
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]
SANDBOX_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/sandbox.py"
MANAGER_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/manager.py"

HAVE_SANDLOCK = shutil.which("sandlock") is not None
HAVE_UV = shutil.which("uv") is not None


class FakeSandboxConfig:
    enabled = True
    memory_limit = "256M"
    environment: dict[str, str] = {}
    workspace_dir_path = Path(tempfile.gettempdir())
    landlock_degrade = "auto"
    landlock_real_binary = None
    uv_bin = "uv"
    uv_python_dir = None
    venv_python = "3.13"
    venv_timeout = 120


def _seed_fake_packages():
    """建立 kchat 假包链与 kchat.config 桩（无条件覆盖，避免跨文件污染）"""
    for name in ("kchat", "kchat.agent"):
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod

    cfg_module = types.ModuleType("kchat.config")
    cfg_module.cfg = types.SimpleNamespace(  # type: ignore[attr-defined]
        sandbox=FakeSandboxConfig()
    )
    cfg_module.CompactionConfig = object  # type: ignore[attr-defined]
    sys.modules["kchat.config"] = cfg_module
    return cfg_module


def _load_sandbox_module(workspace_root: Path | None = None):
    """把 sandbox.py 加载成 kchat.agent.sandbox，注入可控的 cfg"""
    _seed_fake_packages()
    if workspace_root is not None:
        sys.modules["kchat.config"].cfg.sandbox.workspace_dir_path = (  # type: ignore[attr-defined]
            workspace_root
        )

    spec = importlib.util.spec_from_file_location("kchat.agent.sandbox", SANDBOX_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kchat.agent.sandbox"] = module
    spec.loader.exec_module(module)
    return module


def _load_manager_module():
    """把 manager.py 加载成 kchat.agent.manager，桩掉重依赖

    `manager.py` 的相对导入解析到 kchat.agent.* / kchat.config；沙箱模块
    用真身（其依赖已被桩住），其余插件侧依赖全部用最小桩替代。模块底部有
    `chat_manager = ChatSessionManager()` 模块级实例化，因此 cfg、模型
    构造与 nonebot driver 都要给到能跑通 `__init__` 的桩。
    """
    sandbox_mod = _load_sandbox_module()

    cfg = sys.modules["kchat.config"].cfg
    cfg.session = types.SimpleNamespace(db_file_path=Path(tempfile.gettempdir()) / "t.db")  # type: ignore[attr-defined]
    cfg.memory = types.SimpleNamespace(  # type: ignore[attr-defined]
        database_file_path=Path(tempfile.gettempdir()) / "m.db", max_records_per_scope=8
    )
    cfg.compaction = types.SimpleNamespace(  # type: ignore[attr-defined]
        enabled=False, keep_tail=10
    )
    cfg.agent = types.SimpleNamespace(vision=False)  # type: ignore[attr-defined]

    def stub(name: str, **attrs):
        # 无条件覆盖：其他测试文件可能留下同名但形状不同的桩
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod

    class _Obj:
        def __init__(self, *args, **kwargs):
            pass

    class _Prompt:
        static_instructions: list = []

        def __init__(self, *args, **kwargs):
            pass

    async def _noop(*args, **kwargs):
        return None

    stub(
        "kchat.agent.compaction",
        RecordingCompaction=_Obj,
        build_compaction_capability=lambda *a, **k: _Obj(),
        build_summary=lambda *a, **k: _Obj(),
        build_summary_mark=lambda *a, **k: _Obj(),
        extract_summary=lambda *a, **k: None,
    )
    stub("kchat.agent.deps", ChatDeps=_Obj)
    stub("kchat.agent.image_caption", get_image_caption=_noop)
    stub("kchat.agent.memory", MemoryContext=_Obj, MemoryStore=_Obj)
    stub("kchat.agent.prompt", ChatPrompt=_Prompt, current_time_line=lambda: "")
    stub("kchat.agent.session_store", SessionStore=_Obj)
    stub("kchat.agent.tool", build_tools=list)
    stub("kanade_bot.utils.parse", ImageInput=_Obj, build_sender_info=lambda *a, **k: "")
    stub("kanade_bot.utils.session", SessionInfo=_Obj)
    stub("kanade_bot.utils.billing", UsageCallback=_Obj)
    stub(
        "kanade_bot.utils.pai_runtime",
        CONTINUE_PROMPT="",
        build_model_settings=lambda *a, **k: None,
        get_model=lambda *a, **k: None,
    )

    import nonebot

    fake_driver = types.SimpleNamespace(  # type: ignore[attr-defined]
        on_startup=lambda fn: None, on_shutdown=lambda fn: None
    )
    # 补丁只在 exec 期间生效：manager 模块把 get_driver 绑进自己的命名空间，
    # 加载完立刻还原，避免污染同进程里依赖真实 nonebot 的其他测试
    orig_get_driver = nonebot.get_driver
    nonebot.get_driver = lambda: fake_driver  # type: ignore[assignment]
    sandbox_mod.get_driver = lambda: fake_driver  # type: ignore[assignment]
    try:
        spec = importlib.util.spec_from_file_location("kchat.agent.manager", MANAGER_PATH)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["kchat.agent.manager"] = module
        spec.loader.exec_module(module)
    finally:
        nonebot.get_driver = orig_get_driver  # type: ignore[assignment]
    return module, sandbox_mod


def _make_manager(module, root: Path):
    """构造一个不注册 nonebot 生命周期钩子的 SandboxManager"""
    module.get_driver = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        on_startup=lambda fn: None, on_shutdown=lambda fn: None
    )
    return module.SandboxManager()


def _fake_image(name: str, data: bytes | None, mime_type: str | None = "image/png"):
    """构造 ImageInput 形状的对象，data 按 Base64 字符串约定编码"""
    encoded = base64.b64encode(data).decode() if data is not None else None
    return types.SimpleNamespace(name=name, data=encoded, mime_type=mime_type)


# ===== 运行时配置（纯单元，无需 sandlock） =====


class RuntimeConfigTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_sandbox_module()

    def test_font_dirs_authorized(self):
        """宿主上存在的字体目录必须进入只读授权"""
        config = self.module.sandbox_runtime_config(
            Path("/tmp/ws"),
            uv_python_dir=None,
            font_dirs=("/usr/share/fonts", "/usr/local/share/fonts"),
        )
        self.assertIn("/usr/share/fonts", config["fs_readable"])
        self.assertIn("/usr/local/share/fonts", config["fs_readable"])
        self.assertIn("/tmp/ws", config["fs_readable"])

    def test_home_and_xdg_env_injected(self):
        """clean-env 下受限进程需要 HOME/XDG 指向工作区内可写目录"""
        config = self.module.sandbox_runtime_config(Path("/tmp/ws"), None)
        env = config["env"]
        self.assertEqual(env["HOME"], "/tmp/ws/.home")
        self.assertEqual(env["XDG_CACHE_HOME"], "/tmp/ws/.home/.cache")
        self.assertEqual(env["XDG_CONFIG_HOME"], "/tmp/ws/.home/.config")
        self.assertEqual(env["PATH"], "/tmp/ws/.venv/bin")

    def test_uv_python_dir_authorized(self):
        config = self.module.sandbox_runtime_config(Path("/tmp/ws"), uv_python_dir=Path("/opt/py"))
        self.assertIn("/opt/py", config["fs_readable"])

    def test_resolve_font_dirs_only_existing(self):
        """字体目录解析只保留宿主上真实存在的路径"""
        dirs = self.module._resolve_font_dirs()
        for d in dirs:
            self.assertTrue(Path(d).is_dir(), d)


# ===== 图片暂存（纯单元，无需 sandlock） =====


class FakeSandbox:
    """内存版 SandboxSession.read/write"""

    def __init__(self):
        self.files: dict[str, bytes] = {}
        self.writes: list[str] = []

    async def read(self, path: Path) -> BytesIO:
        key = str(path)
        if key not in self.files:
            raise FileNotFoundError(key)
        return BytesIO(self.files[key])

    async def write(self, path: Path, data: BytesIO) -> None:
        self.writes.append(str(path))
        self.files[str(path)] = data.read()


class StageImagesTest(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.manager_mod, _ = _load_manager_module()

    async def stage(self, sandbox, images):
        return await self.manager_mod.ChatSessionManager._stage_images_to_sandbox(sandbox, images)

    async def test_new_image_written(self):
        sandbox = FakeSandbox()
        staged = await self.stage(sandbox, [_fake_image("pic.jpg", b"\x89PNG...")])
        self.assertEqual(staged, ["images/pic.jpg"])
        self.assertEqual(sandbox.files["images/pic.jpg"], b"\x89PNG...")

    async def test_identical_resent_reused(self):
        """同一张图重发：路径复用，不产生第二次写入"""
        sandbox = FakeSandbox()
        img = _fake_image("pic.jpg", b"data")
        await self.stage(sandbox, [img])
        await self.stage(sandbox, [img])
        self.assertEqual(len(sandbox.writes), 1)

    async def test_same_name_different_content_not_overwritten(self):
        """同名不同图：换名另存，旧文件保持原内容"""
        sandbox = FakeSandbox()
        await self.stage(sandbox, [_fake_image("pic.jpg", b"old")])
        staged = await self.stage(sandbox, [_fake_image("pic.jpg", b"new")])
        self.assertEqual(len(staged), 1)
        self.assertNotEqual(staged[0], "images/pic.jpg")
        self.assertTrue(staged[0].startswith("images/"))
        self.assertTrue(staged[0].endswith("_pic.jpg"))
        self.assertEqual(sandbox.files["images/pic.jpg"], b"old")
        self.assertEqual(sandbox.files[staged[0]], b"new")

    async def test_no_data_skipped(self):
        sandbox = FakeSandbox()
        staged = await self.stage(sandbox, [_fake_image("gone.jpg", None)])
        self.assertEqual(staged, [])
        self.assertEqual(sandbox.files, {})

    async def test_name_sanitized_and_suffix_added(self):
        """文件名取 basename防穿越；无后缀时按 mime 补后缀"""
        sandbox = FakeSandbox()
        staged = await self.stage(
            sandbox,
            [
                _fake_image("../../etc/passwd", b"x"),
                _fake_image("noext", b"y", mime_type="image/jpeg"),
            ],
        )
        self.assertIn("images/passwd.png", staged)
        self.assertNotIn("images/etc", staged)
        self.assertIn("images/noext.jpg", staged)

    async def test_accepts_none(self):
        staged = await self.stage(FakeSandbox(), None)
        self.assertEqual(staged, [])


# ===== 端到端：受限进程内的字体与缓存（需要 sandlock + uv） =====


@pytest.mark.skipif(not (HAVE_SANDLOCK and HAVE_UV), reason="sandlock/uv 未安装")
class SandboxedFontAccessTest(unittest.IsolatedAsyncioTestCase):
    """真实 sandlock 受限进程内验证字体可读、缓存可写、隔离未破坏"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.module = _load_sandbox_module(self.root)
        self.addCleanup(self.tmp.cleanup)

    async def test_home_tree_created(self):
        """会话创建时工作区内建好伪HOME目录树"""
        mgr = _make_manager(self.module, self.root)
        session = await mgr.create("fonts")
        try:
            home = session.workspace_dir / ".home"
            self.assertTrue((home / ".cache").is_dir())
            self.assertTrue((home / ".config").is_dir())
        finally:
            await session.close()

    async def test_python_in_sandbox_sees_fonts_and_home(self):
        mgr = _make_manager(self.module, self.root)
        try:
            mgr._check_uv()  # 解析 _uv_bin/_uv_python_dir（正常由 _startup 钩子完成）
        except self.module.UvUnavailableError as e:
            self.skipTest(f"uv 不可用: {e}")
        session = await mgr.create("fonts")
        try:
            report = await session.setup_venv()
            self.assertIn("python3 现在可用", report)

            # mirage 的 SandlockRuntime 固定附加 SYSTEM_READABLE（/usr、/lib、
            # /bin、/etc、/proc、/dev）只读授权，因此界外检查针对宿主用户文件：
            # 授权了 ~/.local/share/fonts 子树后，家目录其余文件必须仍被拒绝
            outside = str(Path.home() / ".bashrc")
            probe = (
                "import os, glob\n"
                "print('HOME=' + os.environ.get('HOME', '<unset>'))\n"
                "print('EXPANDUSER=' + os.path.expanduser('~'))\n"
                "fonts = glob.glob('/usr/share/fonts/**/*.tt[cf]', recursive=True)\n"
                "print('FONT_COUNT=' + str(len(fonts)))\n"
                "data = open(fonts[0], 'rb').read(16) if fonts else b''\n"
                "print('FONT_READ=' + str(len(data)))\n"
                "cache = os.path.join(os.environ['XDG_CACHE_HOME'], 'probe.txt')\n"
                "open(cache, 'w').write('ok')\n"
                "print('CACHE_WRITE=' + open(cache).read())\n"
                "try:\n"
                f"    open({outside!r}).read(8)\n"
                "    print('OUTSIDE=DENIED_EXPECTED_BUT_ALLOWED')\n"
                "except OSError:\n"
                "    print('OUTSIDE=DENIED')\n"
            )
            await session.write(Path("probe.py"), BytesIO(probe.encode()))
            result = await session.backend.run("python3 probe.py", shell=True)
            self.assertEqual(result.exit_code, 0, result.stdout + result.stderr)
            self.assertIn("HOME=" + str(session.workspace_dir / ".home"), result.stdout)
            self.assertIn("CACHE_WRITE=ok", result.stdout)
            self.assertIn("OUTSIDE=DENIED", result.stdout)
            self.assertNotIn("OUTSIDE=DENIED_EXPECTED_BUT_ALLOWED", result.stdout)
            # 宿主装有系统字体时，受限进程必须能读到
            if Path("/usr/share/fonts").is_dir():
                self.assertNotIn("FONT_COUNT=0", result.stdout)
                self.assertNotIn("FONT_READ=0", result.stdout)
        finally:
            await session.close()
