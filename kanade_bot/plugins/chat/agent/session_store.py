"""会话历史持久化（append-only SQLite + 压缩事件标记）。

**唯一真相源是 `messages` 表**：原始消息只追加、永不修改、永不删除。
压缩**不回写** `messages` 表，只以 `compaction_marks` 记录「压缩过哪里 +
用的什么参数 + （摘要档的）产物」。

恢复流程：

```
history = load(conv_id)                      # 全量原始消息
for mark in load_compaction_marks(conv_id): # 按写入顺序
    history = await apply_strategy(history, mark)
⇒ 结果 == 关闭前最后一次请求实际发送的内容
```

两个前提（否则一致性破功）：

1. **参数不能漂移**：恢复时用 mark 里记录的参数，不用当前配置；参数指纹
   不匹配时丢弃旧 marks 并从全量按新参数重放（宁可损失一次缓存也要正确）；
2. **在线压缩与恢复重放共用 `apply_strategy`**（见 `compaction.py`）。

> `messages` 表按「一条消息一行」存储（不是一批一行），便于按 seq 定位与将来做
> 全文检索；每轮 `append` 只写本轮新增的部分。
"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from .compaction import CompactionMark, apply_strategy, build_clear_mark

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    payload BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conv_id, seq);

CREATE TABLE IF NOT EXISTS compaction_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    result BLOB,
    fingerprint TEXT NOT NULL DEFAULT '',
    applied_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_marks_conv ON compaction_marks(conv_id, id);
"""


@dataclass
class SessionStore:
    """`ModelMessage` 全量持久化 + 压缩事件标记"""

    db_path: Path
    """SQLite 数据库路径"""

    _initialized: bool = field(default=False, init=False, repr=False)
    _init_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
                conn.commit()
            self._initialized = True

    # ===== 全量原始消息 =====

    async def load(self, conv_id: str) -> list[ModelMessage]:
        """取全量原始历史（不含压缩）"""
        return await asyncio.to_thread(self._load_sync, conv_id)

    def _load_sync(self, conv_id: str) -> list[ModelMessage]:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE conv_id = ? ORDER BY seq",
                (conv_id,),
            ).fetchall()
        messages: list[ModelMessage] = []
        for (payload,) in rows:
            messages.extend(ModelMessagesTypeAdapter.validate_json(payload))
        return messages

    async def append(self, conv_id: str, msgs: Sequence[ModelMessage]) -> None:
        """追加本轮新增消息（只追加 seq，不改动已有行）"""
        if not msgs:
            return
        await asyncio.to_thread(self._append_sync, conv_id, list(msgs))

    def _append_sync(self, conv_id: str, msgs: list[ModelMessage]) -> None:
        self._ensure_initialized()
        now = time.time()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM messages WHERE conv_id = ?",
                (conv_id,),
            ).fetchone()
            next_seq = (row[0] if row else -1) + 1
            conn.executemany(
                "INSERT INTO messages (conv_id, seq, payload, created_at) VALUES (?, ?, ?, ?)",
                [
                    (
                        conv_id,
                        next_seq + i,
                        ModelMessagesTypeAdapter.dump_json([m]),
                        now,
                    )
                    for i, m in enumerate(msgs)
                ],
            )
            conn.commit()

    async def count(self, conv_id: str) -> int:
        """该会话已存储的消息条数"""
        return await asyncio.to_thread(self._count_sync, conv_id)

    def _count_sync(self, conv_id: str) -> int:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM messages WHERE conv_id = ?", (conv_id,)
            ).fetchone()
        return row[0] if row else 0

    async def truncate(self, conv_id: str, keep: int) -> None:
        """回退到前 keep 条消息（空响应重发时撤销本轮写入）

        这是**唯一**删除消息的路径，且只删本轮刚写入的尾部数据，
        同时清掉本轮产生的压缩事件（它们描述的是已被撤销的历史）。
        """
        await asyncio.to_thread(self._truncate_sync, conv_id, keep)

    def _truncate_sync(self, conv_id: str, keep: int) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM messages WHERE conv_id = ? AND seq >= ?", (conv_id, keep))
            conn.execute("DELETE FROM compaction_marks WHERE conv_id = ?", (conv_id,))
            conn.commit()

    # ===== 压缩事件标记 =====

    async def add_compaction_mark(self, conv_id: str, mark: CompactionMark) -> None:
        """记录一次压缩事件（在线压缩发生后调用）"""
        await asyncio.to_thread(self._add_mark_sync, conv_id, mark)

    def _add_mark_sync(self, conv_id: str, mark: CompactionMark) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute(
                "INSERT INTO compaction_marks "
                "(conv_id, strategy, params, result, fingerprint, applied_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conv_id,
                    mark.strategy,
                    json.dumps(mark.params, ensure_ascii=False),
                    mark.result,
                    mark.fingerprint,
                    mark.applied_at or time.time(),
                ),
            )
            conn.commit()

    async def load_compaction_marks(self, conv_id: str):
        """按写入顺序返回该会话的压缩事件"""
        return await asyncio.to_thread(self._load_marks_sync, conv_id)

    def _load_marks_sync(self, conv_id: str):
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT strategy, params, result, fingerprint, applied_at "
                "FROM compaction_marks WHERE conv_id = ? ORDER BY id",
                (conv_id,),
            ).fetchall()

        return [
            CompactionMark(
                strategy=r[0],
                params=json.loads(r[1]),
                result=r[2],
                fingerprint=r[3],
                applied_at=r[4],
            )
            for r in rows
        ]

    async def clear_marks(self, conv_id: str) -> None:
        """丢弃该会话的全部压缩事件（参数漂移时从全量重放）"""
        await asyncio.to_thread(self._clear_marks_sync, conv_id)

    def _clear_marks_sync(self, conv_id: str) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM compaction_marks WHERE conv_id = ?", (conv_id,))
            conn.commit()

    # ===== 恢复 =====

    async def restore(self, conv_id: str, *, params=None) -> list[ModelMessage]:
        """重建会话历史，结果与关闭前最后一次请求发送的内容一致

        `params` 为当前压缩参数；与 marks 记录的不一致时（配置改过），
        丢弃旧 marks 并按新参数从全量重放，保证自洽。
        """

        history = await self.load(conv_id)
        if not history:
            return history

        marks = await self.load_compaction_marks(conv_id)
        if not marks:
            return history

        if params is not None and any(m.fingerprint != params.fingerprint() for m in marks):
            # 参数漂移：丢弃旧 marks，从全量按新参数重放
            await self.clear_marks(conv_id)
            return await apply_strategy(history, build_clear_mark(params))

        for mark in marks:
            history = await apply_strategy(history, mark)
        return history

    # ===== 清理 =====

    async def clear(self, conv_id: str) -> None:
        """reset：删除该会话的 messages 与 compaction_marks"""
        await asyncio.to_thread(self._clear_sync, conv_id)

    def _clear_sync(self, conv_id: str) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM messages WHERE conv_id = ?", (conv_id,))
            conn.execute("DELETE FROM compaction_marks WHERE conv_id = ?", (conv_id,))
            conn.commit()
