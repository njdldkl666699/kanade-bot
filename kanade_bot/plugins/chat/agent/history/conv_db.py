"""单会话 SQLite 库基础设施：schema、会话库路由、restore 缓存与 blob LRU"""

import hashlib
import re
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter

from .blobs import BlobWrite, externalize_messages

_BLOB_CACHE_MAX_BYTES = 128 * 1024 * 1024
"""单会话 blob LRU 的内存预算（bytes）"""

_SAFE_CONV_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
"""会话 ID 可直接作为文件名时的形态"""

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

CREATE TABLE IF NOT EXISTS gate_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'msg',
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_messages_id ON gate_messages(id);

CREATE TABLE IF NOT EXISTS gate_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def write_blobs(conn: sqlite3.Connection, writes: Sequence[BlobWrite]) -> None:
    if writes:
        conn.executemany(
            "INSERT OR IGNORE INTO blobs (hash, mime, data, created_at) VALUES (?, ?, ?, ?)",
            [(w.hash, w.mime, w.data, time.time()) for w in writes],
        )


def externalize_payload(payload: bytes) -> tuple[bytes, list[BlobWrite]]:
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


class ConvDBRegistry:
    """会话数据库路由：会话 ID → `{session_id}.sqlite3` 与 `_ConvDB` 实例

    主消息表与决策（gate）表同库不同表，`SessionStore` 与 `GateStore`
    共享同一个 registry 实例，共享 per-conv 的缓存与初始化状态。
    """

    def __init__(self, root: Path):
        self.root = root
        self._convs: dict[str, _ConvDB] = {}
        self._lock = Lock()

    def db_path(self, conv_id: str) -> Path:
        name = (
            conv_id
            if _SAFE_CONV_RE.match(conv_id)
            else hashlib.sha256(conv_id.encode()).hexdigest()
        )
        return self.root / f"{name}.sqlite3"

    def get(self, conv_id: str) -> _ConvDB:
        with self._lock:
            db = self._convs.get(conv_id)
            if db is None:
                db = _ConvDB(self.db_path(conv_id))
                self._convs[conv_id] = db
            return db
