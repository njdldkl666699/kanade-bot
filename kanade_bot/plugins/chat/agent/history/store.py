"""会话历史存储：`ModelMessage` 全量持久化 + 压缩事件标记，每会话一库"""

import asyncio
import json
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai import ModelMessage, ModelMessagesTypeAdapter

from ...config import CompactionConfig
from .blobs import BlobWrite, externalize_messages, rehydrate_messages
from .compaction import CompactionMark, apply_strategy, build_clear_mark, messages_fingerprint
from .conv_db import ConvDBRegistry, externalize_payload, write_blobs


@dataclass
class SessionStore:
    """主会话消息与压缩 marks 的 append-only 存储

    决策（gate）表的操作见 `GateStore`；两者经 `registry` 共享同库实例。
    """

    root: Path
    """会话数据库目录，每个会话一个 `{session_id}.sqlite3`"""

    registry: ConvDBRegistry = field(init=False, repr=False)
    """会话库路由（`GateStore` 与本类共享）"""

    def __post_init__(self):
        self.registry = ConvDBRegistry(self.root)

    # ===== 全量原始消息 =====

    async def load(self, conv_id: str) -> list[ModelMessage]:
        """取全量原始历史（不含压缩），二进制内容已还原"""
        return await asyncio.to_thread(self._load_sync, conv_id)

    def _load_sync(self, conv_id: str) -> list[ModelMessage]:
        db = self.registry.get(conv_id)
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
        db = self.registry.get(conv_id)
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
            write_blobs(conn, writes)
            conn.commit()
        db.invalidate_restore_cache()

    async def count(self, conv_id: str) -> int:
        """该会话已存储的消息条数"""
        return await asyncio.to_thread(self._count_sync, conv_id)

    def _count_sync(self, conv_id: str) -> int:
        db = self.registry.get(conv_id)
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
        db = self.registry.get(conv_id)
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
        db = self.registry.get(conv_id)
        db.ensure()
        result: bytes | None = None
        writes: list[BlobWrite] = []
        if mark.result is not None:
            result, writes = externalize_payload(mark.result)
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
            write_blobs(conn, writes)
            conn.commit()
        db.invalidate_restore_cache()

    async def load_compaction_marks(self, conv_id: str) -> list[CompactionMark]:
        """按写入顺序返回该会话的压缩事件（result 已还原二进制内容）"""
        return await asyncio.to_thread(self._load_marks_sync, conv_id)

    def _load_marks_sync(self, conv_id: str) -> list[CompactionMark]:
        db = self.registry.get(conv_id)
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
        db = self.registry.get(conv_id)
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

        `params` 为当前压缩参数；与 marks 记录的指纹不一致（重放相关
        参数漂移）时，丢弃旧 marks 并按新参数从全量重放，重放有实际
        效果时落一条覆盖全量的新 mark，保证后续恢复状态稳定。

        结果缓存为占位形态：命中后只需还原 blobs（LRU 兜底），写路径
        自动失效缓存。
        """
        db = self.registry.get(conv_id)
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
            # 参数漂移（指纹仅覆盖重放相关字段）：丢弃旧 marks，从全量按新参数重放
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
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM compaction_marks")
            conn.execute("DELETE FROM blobs")
            conn.execute("DELETE FROM gate_messages")
            conn.execute("DELETE FROM gate_state")
            conn.commit()
        db.clear_blob_cache()
        db.invalidate_restore_cache()


def _messages_equal(a: list[ModelMessage], b: list[ModelMessage]) -> bool:
    """两组消息是否逐字节相同"""
    return messages_fingerprint(a) == messages_fingerprint(b)
