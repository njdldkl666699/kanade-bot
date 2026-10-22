"""离线分析事故会话库：按生产逻辑 restore，量化事故轮会发出的请求

用法: .venv/bin/python scripts/analyze_413.py [db路径]
导入壳方案抄自 tests/chat/test_pai_agent.py。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

_LOCALSTORE_TMP = Path(tempfile.mkdtemp(prefix="analyze-413-localstore-"))
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

from pydantic_ai import BinaryContent, ModelRequest, UserPromptPart
from pydantic_ai.messages import ModelMessage

from kanade_bot.plugins.chat.agent.session_store import SessionStore
from kanade_bot.plugins.chat.config import CompactionConfig
from kanade_bot.plugins.chat.agent.compaction import estimate_request_tokens

SESSION_ID = "qq-group-875082051"


def analyze_images(messages: list[ModelMessage]) -> tuple[int, int]:
    n = 0
    total = 0
    for m in messages:
        if not isinstance(m, ModelRequest):
            continue
        for part in m.parts:
            if not isinstance(part, UserPromptPart) or isinstance(part.content, str):
                continue
            for item in part.content:
                if isinstance(item, BinaryContent) and item.is_image:
                    n += 1
                    total += len(item.data)
    return n, total


async def main() -> None:
    src = Path(
        sys.argv[1] if len(sys.argv) > 1 else "cache/misc_not_plugin/qq-group-875082051.sqlite3"
    )
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        shutil.copy(src, root / f"{SESSION_ID}.sqlite3")
        store = SessionStore(root=root)
        params = CompactionConfig()

        marks = await store.load_compaction_marks(SESSION_ID)
        print(f"compaction marks: {len(marks)}")
        for mk in marks:
            print(
                f"  - strategy={mk.strategy} up_to_seq={mk.up_to_seq} "
                f"result={'%.1fKB' % (len(mk.result) / 1024) if mk.result else None} "
                f"applied_at={mk.applied_at}"
            )

        raw = await store.load(SESSION_ID)
        n_img, b_img = analyze_images(raw)
        print(f"\n全量原始: {len(raw)} 条, 图片 {n_img} 张 {b_img / 1e6:.1f} MB")

        restored = await store.restore(SESSION_ID, params=params)
        n_img2, b_img2 = analyze_images(restored)
        est = estimate_request_tokens(restored)
        print(f"restore 后: {len(restored)} 条, 图片 {n_img2} 张 {b_img2 / 1e6:.1f} MB")
        print(f"estimate_request_tokens: {est}")
        print(f"图片 base64 体积(请求体主要构成): {b_img2 * 4 / 3 / 1e6:.1f} MB")

        tail = restored[-40:]
        n_tail, b_tail = analyze_images(tail)
        print(
            f"尾部40条(摘要保留范围): 图片 {n_tail} 张 {b_tail / 1e6:.1f} MB "
            f"(base64≈{b_tail * 4 / 3 / 1e6:.1f} MB)"
        )

        positions = [
            (i, len(next(x for x in p.content if isinstance(x, BinaryContent)).data))
            for i, m in enumerate(restored)
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
            and not isinstance(p.content, str)
            and any(isinstance(x, BinaryContent) for x in p.content)
        ]
        print(f"\n含图片消息共 {len(positions)} 条，位置与大小(倒数25条):")
        for i, size in positions[-25:]:
            print(f"  msg[{i}/{len(restored) - 1}] {size / 1024:.0f}KB")


if __name__ == "__main__":
    asyncio.run(main())
