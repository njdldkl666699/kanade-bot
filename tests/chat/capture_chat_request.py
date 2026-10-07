"""通过 openai-proxy 抓取 chat agent 一次完整上游请求体。

用法::

    # 1. 启动代理（cwd 在 openai-proxy/）
    ./openai-proxy -config config-chat-capture.yaml

    # 2. 运行本脚本（cwd 在仓库根）
    .venv/bin/python tests/chat/capture_chat_request.py

    # 3. 查看记录的完整请求体（JSONL，每行一条）
    tests/chat/captured_chat_request.jsonl

初始化方式参照 `tests/chat/test_pai_agent.py`：

- localstore 的 config 目录指向真实 `config/`（系统提示词、chat_configs.json
  均为生产内容），cache/data 指向临时目录（会话库、记忆库不污染真实数据）
- 配置加载复用 `bot.init_nonebot` + `scripts.util.load_configs`，与生产一致
  （chat.agent 即 config-prod.yaml 的 deepseek-official，含 MCP 服务器配置）
- 在导入 manager **之前**把 provider 覆盖为本地代理
  （`manager.py` 模块级会实例化 `chat_manager` 并缓存模型客户端）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from contextlib import aclosing
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

REPO_ROOT = Path(__file__).parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# localstore：config 用真实目录（读生产提示词），cache/data 落临时目录（隔离会话数据）
_TMP = Path(tempfile.mkdtemp(prefix="chat-capture-"))
os.environ.setdefault("LOCALSTORE_CONFIG_DIR", str(REPO_ROOT / "config"))
os.environ.setdefault("LOCALSTORE_CACHE_DIR", str(_TMP / "cache"))
os.environ.setdefault("LOCALSTORE_DATA_DIR", str(_TMP / "data"))

# 与生产完全一致的配置加载（config.yaml + config-prod.yaml）
from scripts.util import load_configs  # noqa: E402

from bot import init_nonebot  # noqa: E402

_env, _configs = load_configs(REPO_ROOT)
init_nonebot(env=_env, **_configs)

# `kanade_bot/plugins/chat/__init__.py` 会把整个 nonebot 插件运行时拉进来，
# 单进程脚本里起不来；登记成只声明子模块路径、不执行 __init__ 的壳
# （与 tests/chat/test_pai_agent.py 相同的做法）
import nonebot  # noqa: E402

_chat_pkg = types.ModuleType("kanade_bot.plugins.chat")
_chat_pkg.__path__ = [str(REPO_ROOT / "kanade_bot" / "plugins" / "chat")]  # type: ignore[attr-defined]
sys.modules.setdefault("kanade_bot.plugins.chat", _chat_pkg)

# chat/config.py require 了 model_updater，必须经 PluginManager 正式加载
nonebot.load_plugin("kanade_bot.plugins.model_updater")

# chat 包是壳模块，chat/config.py 里 localstore 的调用方探测会失败。
# 按调用栈来源分流：kanade_bot.plugins.chat 的调用返回 id 为 "chat" 的 stub
# （localstore 只用 plugin.id_ 拼目录，使提示词/聊天配置落到 config/chat/ 下），
# 其余（model_updater 等）走原探测逻辑
import inspect  # noqa: E402

import nonebot_plugin_localstore as _localstore  # noqa: E402

_orig_try_get_caller_plugin = _localstore._try_get_caller_plugin


class _ChatPluginStub:
    id_ = "chat"
    name = "chat"


def _try_get_caller_plugin_patched():
    frame = inspect.currentframe()
    try:
        while (frame := frame.f_back) is not None:
            module = inspect.getmodule(frame)
            name = module.__name__ if module else None
            if not name or name.split(".", maxsplit=1)[0] == "nonebot_plugin_localstore":
                continue
            if name.startswith("kanade_bot.plugins.chat"):
                return _ChatPluginStub()  # type: ignore[return-value]
            return _orig_try_get_caller_plugin()
    finally:
        del frame
    return _orig_try_get_caller_plugin()


_localstore._try_get_caller_plugin = _try_get_caller_plugin_patched  # type: ignore[assignment]

# 关键：在导入 manager（模块级实例化 chat_manager）之前，把 provider 指向本地代理
from kanade_bot.plugins.chat.config import cfg  # noqa: E402
from kanade_bot.utils.schema import ProviderConfig  # noqa: E402

PROXY_BASE_URL = "http://127.0.0.1:39231/v1"
cfg.agent.provider = ProviderConfig(base_url=PROXY_BASE_URL, api_key="sk-via-openai-proxy")

from kanade_bot.plugins.chat.agent.manager import chat_manager  # noqa: E402
from kanade_bot.utils.session import SessionInfo  # noqa: E402


async def main() -> None:
    # MCP 服务器在 on_startup 钩子里连接，脚本不跑 driver，手动触发；
    # 连接失败只告警，不影响抓取主请求体
    await chat_manager._start_mcp()

    session_info = SessionInfo(
        session_id="capture-private-test",
        nickname="测试用户",
        user_id="10001",
    )
    try:
        async with aclosing(
            chat_manager.send_and_wait(
                session_info,
                "你好，请用一两句话介绍一下你自己。",
                bot_id="10000",
                timeout=180,
            )
        ) as contents:
            async for content in contents:
                print(f"assistant> {content}")
    finally:
        await chat_manager._shutdown()

    record = REPO_ROOT / "tests" / "chat" / "captured_chat_request.jsonl"
    print(f"\n请求体已记录至: {record}")


asyncio.run(main())
