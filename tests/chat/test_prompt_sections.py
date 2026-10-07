"""提示词会话层条件拼接：Python 环境段随可用性禁用

`sandbox_python.md` 独立成段（`when=python_env_available`）：uv 与宿主
python3 全部缺失时不给模型任何 Python 环境的引导。
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
PROMPT_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/prompt.py"
PROMPTS_DIR = REPO_ROOT / "config/chat/prompts"


def _load_prompt_module():
    """把 prompt.py 加载成 kchat.agent.prompt，桩掉重依赖"""
    for name in ("kchat", "kchat.agent", "kanade_bot", "kanade_bot.utils"):
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod

    parse_mod = types.ModuleType("kanade_bot.utils.parse")
    parse_mod.build_sender_info = lambda *a, **k: ""  # type: ignore[attr-defined]
    sys.modules["kanade_bot.utils.parse"] = parse_mod

    deps_mod = types.ModuleType("kchat.agent.deps")
    deps_mod.ChatDeps = object  # type: ignore[attr-defined]
    sys.modules["kchat.agent.deps"] = deps_mod

    cfg_module = types.ModuleType("kchat.config")
    cfg_module.cfg = types.SimpleNamespace(  # type: ignore[attr-defined]
        agent=types.SimpleNamespace(vision=False, model="", prompt=None),
        tts=types.SimpleNamespace(base_url=""),
        rag=types.SimpleNamespace(enabled=False),
    )
    cfg_module.chat_configs = types.SimpleNamespace(instance=types.SimpleNamespace(memes={}))  # type: ignore[attr-defined]
    cfg_module.ChatPromptConfig = object  # type: ignore[attr-defined]
    cfg_module.ScopedConfig = object  # type: ignore[attr-defined]
    sys.modules["kchat.config"] = cfg_module

    spec = importlib.util.spec_from_file_location("kchat.agent.prompt", PROMPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kchat.agent.prompt"] = module
    spec.loader.exec_module(module)
    return module


class _Section:
    """PromptSectionConfig 的最小替身（PromptRenderer 只读 file/when）"""

    def __init__(self, file: str, when: str = ""):
        self.file = file
        self.when = when


class PromptSectionRenderTest(unittest.TestCase):
    def setUp(self):
        self.module = _load_prompt_module()

    def _render(self, **variables: str) -> str:
        cfg = types.SimpleNamespace(
            dir="prompts/",
            files=[],
            vars={},
            sections=[
                _Section("sandbox.md", "sandbox_enabled"),
                _Section("sandbox_python.md", "python_env_available"),
                _Section("group_chat.md", "is_group"),
            ],
        )
        renderer = self.module.PromptRenderer(cfg, root=PROMPTS_DIR)  # type: ignore[arg-type]
        return renderer.render_sections(variables)

    def test_python_section_hidden_when_unavailable(self):
        """uv 与宿主 python3 全缺失时，Python 环境段不出现"""
        rendered = self._render(sandbox_enabled="true", python_env_available="false")
        self.assertIn("# 沙箱工作区", rendered)
        self.assertNotIn("setup_python_env", rendered)
        self.assertNotIn("Python 环境", rendered)

    def test_python_section_shown_when_available(self):
        rendered = self._render(sandbox_enabled="true", python_env_available="true")
        self.assertIn("# 沙箱工作区", rendered)
        self.assertIn("setup_python_env", rendered)

    def test_sandbox_disabled_hides_both(self):
        """沙箱整体关闭时两段都不出现

        渲染器对各段独立门控；生产语义由 `_session_vars` 保证：
        `python_env_available` 的计算已含 `deps.sandbox is not None`，
        沙箱关闭时该变量必为 false。
        """
        rendered = self._render(sandbox_enabled="false", python_env_available="false")
        self.assertNotIn("# 沙箱工作区", rendered)
        self.assertNotIn("setup_python_env", rendered)
        self.assertEqual(rendered, "")


class PythonEnvVarTest(unittest.TestCase):
    """`ChatPrompt._session_vars` 的 python_env_available 耦合逻辑"""

    @classmethod
    def setUpClass(cls):
        import nonebot

        # 同进程重复初始化会抛错：已初始化则直接复用现有 driver
        try:
            nonebot.init(driver="~none")
        except Exception:  # noqa: S110 -- 复用已初始化的 nonebot，无需处理
            pass
        cls.module = _load_prompt_module()
        cls.prompt = cls.module.ChatPrompt(
            types.SimpleNamespace(  # type: ignore[arg-type]
                agent=types.SimpleNamespace(
                    vision=False,
                    model="",
                    prompt=types.SimpleNamespace(
                        dir_path=PROMPTS_DIR,
                        dir="prompts/",
                        files=[],
                        vars={},
                        sections=[],
                    ),
                )
            )
        )

    def _vars(self, sandbox: object | None) -> dict[str, str]:
        deps = types.SimpleNamespace(
            session_info=types.SimpleNamespace(
                platform="qq",
                session_id="test",
                nickname="tester",
                user_id="1",
                group_name=None,
                group_id=None,
            ),
            memory_context=None,
            sandbox=sandbox,
            sandbox_root=None,
        )
        return self.prompt._session_vars(deps)  # type: ignore[arg-type]

    def test_no_python_capability_disables_var(self):
        """uv 与宿主 python3 全缺失 → python_env_available=false"""
        sandbox = types.SimpleNamespace(uv_bin=None, host_python=None)
        self.assertEqual(self._vars(sandbox)["python_env_available"], "false")

    def test_uv_or_host_python_enables_var(self):
        for sandbox in (
            types.SimpleNamespace(uv_bin="/usr/bin/uv", host_python=None),
            types.SimpleNamespace(uv_bin=None, host_python="/usr/bin/python3"),
        ):
            self.assertEqual(self._vars(sandbox)["python_env_available"], "true")

    def test_sandbox_none_disables_var(self):
        self.assertEqual(self._vars(None)["python_env_available"], "false")


if __name__ == "__main__":
    unittest.main()
