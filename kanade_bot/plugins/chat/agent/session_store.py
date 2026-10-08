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

from kanade_bot.plugins.chat.config import CompactionConfig

from .compaction import CompactionMark, _messages_fingerprint, apply_strategy, build_clear_mark


def _messages_equal(a: list[ModelMessage], b: list[ModelMessage]) -> bool:
    """两组消息是否逐字节相同"""
    return _messages_fingerprint(a) == _messages_fingerprint(b)


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
    applied_at REAL NOT NULL,
    up_to_seq INTEGER NOT NULL DEFAULT -1
);
CREATE INDEX IF NOT EXISTS idx_marks_conv ON compaction_marks(conv_id, id);
"""

_SCHEMA_MIGRATIONS = (
    (
        "compaction_marks",
        "up_to_seq",
        "ALTER TABLE compaction_marks ADD COLUMN up_to_seq INTEGER NOT NULL DEFAULT -1",
    ),
)
"""旧库迁移：表已存在但缺列时补齐（DEFAULT -1 = 覆盖全量的旧语义）"""


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
                for table, column, ddl in _SCHEMA_MIGRATIONS:
                    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                    if column not in columns:
                        conn.execute(ddl)
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

    async def rollback(self, conv_id: str, first_seq: int) -> None:
        """回退到 seq < first_seq 的状态

        这是**唯一**删除消息的路径，且只删本轮刚写入的尾部数据：删除
        `seq >= first_seq` 的消息，以及 `up_to_seq >= first_seq` 的压缩
        事件（本轮产生的）。「消息条数」在存在压缩时不等于 seq，调用方
        必须传**行号**（轮次开始时的 `count()`），不能传消息条数。
        """
        await asyncio.to_thread(self._rollback_sync, conv_id, first_seq)

    def _rollback_sync(self, conv_id: str, first_seq: int) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute(
                "DELETE FROM messages WHERE conv_id = ? AND seq >= ?", (conv_id, first_seq)
            )
            conn.execute(
                "DELETE FROM compaction_marks WHERE conv_id = ? AND up_to_seq >= ?",
                (conv_id, first_seq),
            )
            conn.commit()

    # ===== 压缩事件标记 =====

    async def add_compaction_mark(self, conv_id: str, mark: CompactionMark) -> None:
        """记录一次压缩事件（`up_to_seq` 为覆盖到的消息 seq，含）"""
        await asyncio.to_thread(self._add_mark_sync, conv_id, mark)

    def _add_mark_sync(self, conv_id: str, mark: CompactionMark) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute(
                "INSERT INTO compaction_marks "
                "(conv_id, strategy, params, result, fingerprint, applied_at, up_to_seq) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    conv_id,
                    mark.strategy,
                    json.dumps(mark.params, ensure_ascii=False),
                    mark.result,
                    mark.fingerprint,
                    mark.applied_at or time.time(),
                    mark.up_to_seq,
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
                "SELECT strategy, params, result, fingerprint, applied_at, up_to_seq "
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
                up_to_seq=r[5],
            )
            for r in rows
        ]

    async def clear_marks(self, conv_id: str) -> None:
        """丢弃该会话的全部压缩事件"""
        await asyncio.to_thread(self._clear_marks_sync, conv_id)

    def _clear_marks_sync(self, conv_id: str) -> None:
        self._ensure_initialized()
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM compaction_marks WHERE conv_id = ?", (conv_id,))
            conn.commit()

    # ===== 恢复 =====

    async def restore(self, conv_id: str, *, params: CompactionConfig | None = None):
        """重建会话历史，结果与关闭前最后一次请求发送的内容一致

        按 mark 的 `up_to_seq` 分段重放：每条 mark 只应用到它覆盖的
        前缀上，其后追加的消息原样拼回——否则 mark 之后的新消息会被
        整体替换掉（历史丢失）。

        `params` 为当前压缩参数；与 marks 记录的不一致（配置漂移）时，
        丢弃旧 marks 并按新参数从全量重放，重放有实际效果时落一条
        覆盖全量的新 mark，保证后续恢复状态稳定。
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
            replayed = await apply_strategy(history, build_clear_mark(params))
            if _messages_equal(replayed, history):
                return replayed
            mark = build_clear_mark(params)
            mark.up_to_seq = len(history) - 1
            await self.add_compaction_mark(conv_id, mark)
            return replayed

        acc: list[ModelMessage] = []
        covered = -1
        for mark in marks:
            boundary = mark.up_to_seq if mark.up_to_seq >= 0 else len(history) - 1
            segment = history[covered + 1 : boundary + 1]
            acc = await apply_strategy([*acc, *segment], mark)
            covered = boundary
        return [*acc, *history[covered + 1 :]]

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
