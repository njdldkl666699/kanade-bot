"""最终还原：用事故时生产参数（来自 mark.params）重放 drift 路径，量化被复活的请求体"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")
_LOCALSTORE_TMP = Path(tempfile.mkdtemp(prefix="analyze-413c-"))
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

import sqlite3

from pydantic_ai import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.messages import ModelMessage, ToolReturnPart
from pydantic_ai_harness.compaction import estimate_context_tokens

from kanade_bot.plugins.chat.agent.session_store import SessionStore
from kanade_bot.plugins.chat.config import CompactionConfig

SESSION_ID = "qq-group-875082051"
SRC = Path(sys.argv[1] if len(sys.argv) > 1 else "cache/misc_not_plugin/qq-group-875082051.sqlite3")

# 事故时生产参数（从 mark.params 提取）
with sqlite3.connect(f"file:{SRC}?mode=ro", uri=True) as conn:
    mark_params = json.loads(
        conn.execute("SELECT params FROM compaction_marks ORDER BY id").fetchone()[0]
    )
PROD = CompactionConfig.model_validate(mark_params)
print("事故时生产参数:", mark_params)


def stats(tag: str, messages: list[ModelMessage]) -> None:
    img_n = img_b = tool_b = text_b = 0
    for m in messages:
        if not isinstance(m, ModelRequest):
            continue
        for p in m.parts:
            if isinstance(p, UserPromptPart) and not isinstance(p.content, str):
                for item in p.content:
                    if isinstance(item, BinaryContent):
                        img_n += 1
                        img_b += len(item.data)
                    elif isinstance(item, str):
                        text_b += len(item)
            elif isinstance(p, UserPromptPart):
                text_b += len(p.content)
            elif isinstance(p, ToolReturnPart):
                tool_b += len(str(p.content))
    body = (img_b * 4 // 3 + text_b + tool_b) / 1e6
    print(
        f"{tag}: {len(messages)}条 | 图片{img_n}张/{img_b / 1e6:.0f}MB"
        f" | 文本{text_b / 1e6:.0f}MB | 工具返回{tool_b / 1e6:.0f}MB"
        f" | 请求体≈{body:.0f}MB | est={estimate_context_tokens(messages)}"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copy(SRC, root / f"{SESSION_ID}.sqlite3")
        store = SessionStore(root=root)

        raw = await store.load(SESSION_ID)
        stats("全量原始(事故时被复活的内容)", raw)

        restored = await store.restore(SESSION_ID, params=PROD)
        stats("生产参数重放后(实际发送内容)", restored)

        # 若无漂移、老摘要 marks 仍在，正常情况下上下文应只有摘要+尾部
        # 用最后锚点 input≈260K tokens 反推：约 0.4~1MB 文本+少量图
        print("\n对照：最后成功请求 input_tokens=260726 → 正常请求体应为个位数 MB")


if __name__ == "__main__":
    asyncio.run(main())
