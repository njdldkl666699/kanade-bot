"""会话历史存储：每会话一个 SQLite 库，消息 JSON + 外置 blobs + restore 缓存"""

import asyncio
import hashlib
import json
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter

from ..config import CompactionConfig
from .compaction import CompactionMark, _messages_fingerprint, apply_strategy, build_clear_mark
from .message_blobs import BlobWrite, externalize_messages, rehydrate_messages

_BLOB_CACHE_MAX_BYTES = 128 * 1024 * 1024
"""单会话 blob LRU 的内存预算（bytes）"""

_SAFE_CONV_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
"""会话 ID 可直接作为文件名时的形态（QQ 群/私聊会话 ID 均满足）"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    payload BLOB NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_seq ON messages(seq);

CREATE TABLE IF NOT EXISTS compaction_marks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT NOT NULL,
    params TEXT NOT NULL,
    result BLOB,
    fingerprint TEXT NOT NULL DEFAULT '',
    applied_at REAL NOT NULL,
    up_to_seq INTEGER NOT NULL DEFAULT -1
);

CREATE TABLE IF NOT EXISTS blobs (
    hash TEXT PRIMARY KEY,
    mime TEXT NOT NULL,
    data BLOB NOT NULL,
    created_at REAL NOT NULL
);
"""


def _write_blobs(conn: sqlite3.Connection, writes: Sequence[BlobWrite]) -> None:
    if writes:
        conn.executemany(
            "INSERT OR IGNORE INTO blobs (hash, mime, data, created_at) VALUES (?, ?, ?, ?)",
            [(w.hash, w.mime, w.data, time.time()) for w in writes],
        )


def _externalize_payload(payload: bytes) -> tuple[bytes, list[BlobWrite]]:
    """把一段已序列化的消息 JSON 外置化（mark result 写入时复用）"""
    messages = ModelMessagesTypeAdapter.validate_json(payload)
    externalized, writes = externalize_messages(messages)
    return ModelMessagesTypeAdapter.dump_json(externalized), writes


@dataclass
class _ConvDB:
    """单会话数据库实例：schema、blob LRU 与 restore 缓存"""

    db_path: Path

    _initialized: bool = field(default=False, init=False, repr=False)
    _init_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    _state_lock: Lock = field(default_factory=Lock, init=False, repr=False)
    """保护 restore 缓存与 blob LRU（跨线程防御；正常流量由会话锁串行）"""

    _restore_cache: tuple[str | None, list[ModelMessage]] | None = field(
        default=None, init=False, repr=False
    )
    """restore 结果缓存：(参数指纹, 占位形态消息)；写路径置 None 失效"""

    _blob_cache: OrderedDict[str, bytes] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _blob_cache_bytes: int = field(default=0, init=False, repr=False)

    def ensure(self) -> None:
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

    # ===== 缓存 =====

    def clear_blob_cache(self) -> None:
        with self._state_lock:
            self._blob_cache.clear()
            self._blob_cache_bytes = 0

    def invalidate_restore_cache(self) -> None:
        with self._state_lock:
            self._restore_cache = None

    def take_restore_cache(self, fingerprint: str | None) -> list[ModelMessage] | None:
        with self._state_lock:
            cached = self._restore_cache
            if cached is None or cached[0] != fingerprint:
                return None
            return cached[1]

    def put_restore_cache(self, fingerprint: str | None, messages: list[ModelMessage]) -> None:
        with self._state_lock:
            self._restore_cache = (fingerprint, messages)

    def fetch_blob(self, blob_hash: str) -> bytes | None:
        """LRU 优先的 blob 读取；库里也没有时返回 None"""
        with self._state_lock:
            data = self._blob_cache.get(blob_hash)
            if data is not None:
                self._blob_cache.move_to_end(blob_hash)
                return data
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            row = conn.execute("SELECT data FROM blobs WHERE hash = ?", (blob_hash,)).fetchone()
        if row is None:
            return None
        data = row[0]
        with self._state_lock:
            if blob_hash not in self._blob_cache:
                self._blob_cache[blob_hash] = data
                self._blob_cache_bytes += len(data)
            else:
                self._blob_cache.move_to_end(blob_hash)
            while self._blob_cache_bytes > _BLOB_CACHE_MAX_BYTES and len(self._blob_cache) > 1:
                _, evicted = self._blob_cache.popitem(last=False)
                self._blob_cache_bytes -= len(evicted)
        return data


@dataclass
class SessionStore:
    """`ModelMessage` 全量持久化 + 压缩事件标记，每会话一库"""

    root: Path
    """会话数据库目录，每个会话一个 `{session_id}.sqlite3`"""

    _convs: dict[str, _ConvDB] = field(default_factory=dict, init=False, repr=False)
    _conv_lock: Lock = field(default_factory=Lock, init=False, repr=False)

    # ===== 会话库路由 =====

    def _conv_db_path(self, conv_id: str) -> Path:
        name = (
            conv_id
            if _SAFE_CONV_RE.match(conv_id)
            else hashlib.sha256(conv_id.encode()).hexdigest()
        )
        return self.root / f"{name}.sqlite3"

    def _conv_db(self, conv_id: str) -> _ConvDB:
        with self._conv_lock:
            db = self._convs.get(conv_id)
            if db is None:
                db = _ConvDB(self._conv_db_path(conv_id))
                self._convs[conv_id] = db
            return db

    # ===== 全量原始消息 =====

    async def load(self, conv_id: str) -> list[ModelMessage]:
        """取全量原始历史（不含压缩），二进制内容已还原"""
        return await asyncio.to_thread(self._load_sync, conv_id)

    def _load_sync(self, conv_id: str) -> list[ModelMessage]:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            rows = conn.execute("SELECT payload FROM messages ORDER BY seq").fetchall()
        messages: list[ModelMessage] = []
        for (payload,) in rows:
            messages.extend(ModelMessagesTypeAdapter.validate_json(payload))
        return rehydrate_messages(messages, db.fetch_blob)

    async def append(self, conv_id: str, msgs: Sequence[ModelMessage]) -> None:
        """追加本轮新增消息（只追加 seq，不改动已有行）"""
        if not msgs:
            return
        await asyncio.to_thread(self._append_sync, conv_id, list(msgs))

    def _append_sync(self, conv_id: str, msgs: list[ModelMessage]) -> None:
        db = self._conv_db(conv_id)
        db.ensure()
        externalized, writes = externalize_messages(msgs)
        now = time.time()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), -1) FROM messages").fetchone()
            next_seq = (row[0] if row else -1) + 1
            conn.executemany(
                "INSERT INTO messages (seq, payload, created_at) VALUES (?, ?, ?)",
                [
                    (next_seq + i, ModelMessagesTypeAdapter.dump_json([m]), now)
                    for i, m in enumerate(externalized)
                ],
            )
            _write_blobs(conn, writes)
            conn.commit()
        db.invalidate_restore_cache()

    async def count(self, conv_id: str) -> int:
        """该会话已存储的消息条数"""
        return await asyncio.to_thread(self._count_sync, conv_id)

    def _count_sync(self, conv_id: str) -> int:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        return row[0] if row else 0

    async def rollback(self, conv_id: str, first_seq: int) -> None:
        """回退到 seq < first_seq 的状态

        这是**唯一**删除消息的路径，且只删本轮刚写入的尾部数据：删除
        `seq >= first_seq` 的消息，以及 `up_to_seq >= first_seq` 的压缩
        事件（本轮产生的）。「消息条数」在存在压缩时不等于 seq，调用方
        必须传**行号**（轮次开始时的 `count()`），不能传消息条数。
        被删消息引用的 blobs 成为孤儿，量小且随会话 reset 回收，暂不追踪。
        """
        await asyncio.to_thread(self._rollback_sync, conv_id, first_seq)

    def _rollback_sync(self, conv_id: str, first_seq: int) -> None:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM messages WHERE seq >= ?", (first_seq,))
            conn.execute("DELETE FROM compaction_marks WHERE up_to_seq >= ?", (first_seq,))
            conn.commit()
        db.invalidate_restore_cache()

    # ===== 压缩事件标记 =====

    async def add_compaction_mark(self, conv_id: str, mark: CompactionMark) -> None:
        """记录一次压缩事件（`up_to_seq` 为覆盖到的消息 seq，含）

        `mark.result` 为还原形态的消息 JSON；入库前外置化其中的二进制内容
        """
        await asyncio.to_thread(self._add_mark_sync, conv_id, mark)

    def _add_mark_sync(self, conv_id: str, mark: CompactionMark) -> None:
        db = self._conv_db(conv_id)
        db.ensure()
        result: bytes | None = None
        writes: list[BlobWrite] = []
        if mark.result is not None:
            result, writes = _externalize_payload(mark.result)
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute(
                "INSERT INTO compaction_marks "
                "(strategy, params, result, fingerprint, applied_at, up_to_seq) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    mark.strategy,
                    json.dumps(mark.params, ensure_ascii=False),
                    result,
                    mark.fingerprint,
                    mark.applied_at or time.time(),
                    mark.up_to_seq,
                ),
            )
            _write_blobs(conn, writes)
            conn.commit()
        db.invalidate_restore_cache()

    async def load_compaction_marks(self, conv_id: str) -> list[CompactionMark]:
        """按写入顺序返回该会话的压缩事件（result 已还原二进制内容）"""
        return await asyncio.to_thread(self._load_marks_sync, conv_id)

    def _load_marks_sync(self, conv_id: str) -> list[CompactionMark]:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT strategy, params, result, fingerprint, applied_at, up_to_seq "
                "FROM compaction_marks ORDER BY id"
            ).fetchall()

        marks: list[CompactionMark] = []
        for r in rows:
            result: bytes | None = None
            if r[2] is not None:
                messages = ModelMessagesTypeAdapter.validate_json(r[2])
                result = ModelMessagesTypeAdapter.dump_json(
                    rehydrate_messages(messages, db.fetch_blob)
                )
            marks.append(
                CompactionMark(
                    strategy=r[0],
                    params=json.loads(r[1]),
                    result=result,
                    fingerprint=r[3],
                    applied_at=r[4],
                    up_to_seq=r[5],
                )
            )
        return marks

    async def clear_marks(self, conv_id: str) -> None:
        """丢弃该会话的全部压缩事件"""
        await asyncio.to_thread(self._clear_marks_sync, conv_id)

    def _clear_marks_sync(self, conv_id: str) -> None:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM compaction_marks")
            conn.commit()
        db.invalidate_restore_cache()

    # ===== 恢复 =====

    async def restore(self, conv_id: str, *, params: CompactionConfig | None = None):
        """重建会话历史，结果与关闭前最后一次请求发送的内容一致

        按 mark 的 `up_to_seq` 分段重放：每条 mark 只应用到它覆盖的
        前缀上，其后追加的消息原样拼回——否则 mark 之后的新消息会被
        整体替换掉（历史丢失）。

        `params` 为当前压缩参数；与 marks 记录的不一致（配置漂移）时，
        丢弃旧 marks 并按新参数从全量重放，重放有实际效果时落一条
        覆盖全量的新 mark，保证后续恢复状态稳定。

        结果缓存为占位形态：命中后只需还原 blobs（LRU 兜底），写路径
        自动失效缓存。
        """
        db = self._conv_db(conv_id)
        fingerprint = params.fingerprint() if params is not None else None
        if (cached := db.take_restore_cache(fingerprint)) is not None:
            return rehydrate_messages(cached, db.fetch_blob)

        history = await self.load(conv_id)
        if not history:
            return history

        marks = await self.load_compaction_marks(conv_id)
        if not marks:
            final = history
        elif params is not None and any(m.fingerprint != params.fingerprint() for m in marks):
            # 参数漂移：丢弃旧 marks，从全量按新参数重放
            await self.clear_marks(conv_id)
            replayed = await apply_strategy(history, build_clear_mark(params))
            if not _messages_equal(replayed, history):
                mark = build_clear_mark(params)
                mark.up_to_seq = len(history) - 1
                await self.add_compaction_mark(conv_id, mark)
            final = replayed
        else:
            acc: list[ModelMessage] = []
            covered = -1
            for mark in marks:
                boundary = mark.up_to_seq if mark.up_to_seq >= 0 else len(history) - 1
                segment = history[covered + 1 : boundary + 1]
                acc = await apply_strategy([*acc, *segment], mark)
                covered = boundary
            final = [*acc, *history[covered + 1 :]]

        externalized, _ = await asyncio.to_thread(externalize_messages, final)
        db.put_restore_cache(fingerprint, externalized)
        return final

    # ===== 清理 =====

    async def clear(self, conv_id: str) -> None:
        """reset：删除该会话的全部消息、压缩事件与 blobs"""
        await asyncio.to_thread(self._clear_sync, conv_id)

    def _clear_sync(self, conv_id: str) -> None:
        db = self._conv_db(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM compaction_marks")
            conn.execute("DELETE FROM blobs")
            conn.commit()
        db.clear_blob_cache()
        db.invalidate_restore_cache()


def _messages_equal(a: list[ModelMessage], b: list[ModelMessage]) -> bool:
    """两组消息是否逐字节相同"""
    return _messages_fingerprint(a) == _messages_fingerprint(b)
