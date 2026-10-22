"""补充分析：事故前的原始历史 token 压力、最后 usage 锚点、mark 参数指纹对比"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
_LOCALSTORE_TMP = Path(tempfile.mkdtemp(prefix="analyze-413b-"))
os.environ.setdefault("LOCALSTORE_CONFIG_DIR", str(_LOCALSTORE_TMP / "config"))
os.environ.setdefault("LOCALSTORE_CACHE_DIR", str(_LOCALSTORE_TMP / "cache"))
os.environ.setdefault("LOCALSTORE_DATA_DIR", str(_LOCALSTORE_TMP / "data"))
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
import nonebot

nonebot.init()
_chat_pkg = types.ModuleType("kanade_bot.plugins.chat")
_chat_pkg.__path__ = [str(REPO_ROOT / "kanade_bot" / "plugins" / "chat")]  # type: ignore[attr-defined]
sys.modules.setdefault("kanade_bot.plugins.chat", _chat_pkg)
nonebot.load_plugin("kanade_bot.plugins.model_updater")
import nonebot_plugin_localstore as _localstore

_localstore._try_get_caller_plugin = lambda: nonebot.get_plugin("model_updater")  # type: ignore[assignment]

from pydantic_ai import BinaryContent, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.messages import ModelMessage
from pydantic_ai_harness.compaction import estimate_context_tokens

from kanade_bot.plugins.chat.agent.session_store import SessionStore
from kanade_bot.plugins.chat.config import CompactionConfig

SESSION_ID = "qq-group-875082051"
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "cache/misc_not_plugin/qq-group-875082051.sqlite3")


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copy(SRC, root / f"{SESSION_ID}.sqlite3")
        store = SessionStore(root=root)

        # mark 参数 vs 当前默认参数指纹
        with sqlite3.connect(root / f"{SESSION_ID}.sqlite3") as conn:
            row = conn.execute(
                "SELECT params, fingerprint FROM compaction_marks ORDER BY id"
            ).fetchone()
        mark_params = json.loads(row[0])
        current = CompactionConfig()
        print("mark fingerprint == 当前默认指纹:", row[1] == current.fingerprint())
        diff = {
            k: (mark_params.get(k), getattr(current, k))
            for k in set(mark_params) | set(current.model_dump())
            if mark_params.get(k) != getattr(current, k, None)
        }
        print("mark 参数与当前默认差异:", diff or "无")

        raw = await store.load(SESSION_ID)
        n_req = sum(isinstance(m, ModelRequest) for m in raw)
        n_resp = sum(isinstance(m, ModelResponse) for m in raw)
        print(f"\n原始(未应用mark): {len(raw)} 条 = {n_req} 请求 + {n_resp} 响应")
        print(f"estimate_context_tokens(原始): {estimate_context_tokens(raw)}")
        print(
            f"estimate_context_tokens(已清工具结果): {estimate_context_tokens(await store.restore(SESSION_ID, params=current))}"
        )

        # 最后几个 ModelResponse 的 usage 锚点（真实请求 input）
        anchors = [
            (i, m.usage.input_tokens, m.usage.output_tokens)
            for i, m in enumerate(raw)
            if isinstance(m, ModelResponse) and m.usage.input_tokens
        ]
        print(f"\n含 usage 的响应数: {len(anchors)}，最后5个 (位置, input, output):")
        for a in anchors[-5:]:
            print(f"  {a}")

        # 各类型 part 体量
        from collections import Counter

        kinds = Counter(type(p).__name__ for m in raw for p in getattr(m, "parts", ()))
        print("\npart 构成:", dict(kinds))


if __name__ == "__main__":
    asyncio.run(main())
