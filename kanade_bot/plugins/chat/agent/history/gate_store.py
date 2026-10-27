"""主动回复决策历史的库表操作"""

import asyncio
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass

from .conv_db import ConvDBRegistry


@dataclass
class GateHistorySnapshot:
    """决策消息历史的生效序列快照（重启加载结果）"""

    rows: list[tuple[int, str, str]]
    """生效行 `(id, role, content)`，当前摘要锚点行（若有）在最前"""

    watermark: int
    """生效水位线：普通行（kind='msg'）的起始行号；之前的行保留可回溯但不参与请求"""

    anchor_at: float | None
    """当前生效摘要锚点的生成时间；无锚点时为 None"""

    total_rows: int
    """全量行数（含水位线之前与旧锚点，统计观测用）"""


@dataclass
class GateStore:
    """决策历史存储：与主会话消息同库（经共享 `ConvDBRegistry` 路由）"""

    registry: ConvDBRegistry

    async def append_gate_messages(
        self, conv_id: str, entries: Sequence[tuple[str, str, str]]
    ) -> list[int]:
        """决策消息历史全量 append；截断不删行，只推进水位线

        :param entries: `(kind, role, content)` 列表；kind 为 'msg'（普通行）
            或 'anchor'（摘要锚点行，role 恒为 'user'）
        :returns: 新写入的行 id 列表（与 entries 等长、顺序一致）
        """
        return await asyncio.to_thread(self._append_gate_sync, conv_id, list(entries))

    def _append_gate_sync(self, conv_id: str, entries: list[tuple[str, str, str]]) -> list[int]:
        db = self.registry.get(conv_id)
        db.ensure()
        new_rows: list[int] = []
        now = time.time()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            for kind, role, content in entries:
                cursor = conn.execute(
                    "INSERT INTO gate_messages (kind, role, content, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (kind, role, content, now),
                )
                new_rows.append(cursor.lastrowid or 0)
            conn.commit()
        return new_rows

    async def load_gate_history(self, conv_id: str) -> GateHistorySnapshot:
        """加载决策消息历史的生效序列：最新锚点行 + 水位线之后的普通行

        水位线之前的全量行保留在库中可回溯，不参与请求——重启后请求前缀
        与关机前完全一致，前缀缓存跨重启命中。
        """
        return await asyncio.to_thread(self._load_gate_sync, conv_id)

    def _load_gate_sync(self, conv_id: str) -> GateHistorySnapshot:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            watermark_row = conn.execute(
                "SELECT value FROM gate_state WHERE key = 'watermark'"
            ).fetchone()
            watermark = int(watermark_row[0]) if watermark_row else 0

            total = conn.execute("SELECT COUNT(*) FROM gate_messages").fetchone()[0]

            anchor = conn.execute(
                "SELECT id, content, created_at FROM gate_messages "
                "WHERE kind = 'anchor' ORDER BY id DESC LIMIT 1"
            ).fetchone()

            msg_rows = conn.execute(
                "SELECT id, role, content FROM gate_messages "
                "WHERE kind = 'msg' AND id >= ? ORDER BY id",
                (watermark,),
            ).fetchall()

        rows: list[tuple[int, str, str]] = []
        anchor_at: float | None = None
        if anchor is not None:
            rows.append((anchor[0], "user", anchor[1]))
            anchor_at = anchor[2]
        rows.extend(msg_rows)
        return GateHistorySnapshot(
            rows=rows, watermark=watermark, anchor_at=anchor_at, total_rows=total
        )

    async def get_gate_state(self, conv_id: str, key: str) -> str | None:
        """读决策会话状态 kv；无该键时返回 None"""
        return await asyncio.to_thread(self._get_gate_state_sync, conv_id, key)

    def _get_gate_state_sync(self, conv_id: str, key: str) -> str | None:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            row = conn.execute("SELECT value FROM gate_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    async def set_gate_state(self, conv_id: str, key: str, value: str) -> None:
        """写决策会话状态 kv（水位线、上次发言时间戳等，UPSERT 语义）"""
        await asyncio.to_thread(self._set_gate_state_sync, conv_id, key, value)

    def _set_gate_state_sync(self, conv_id: str, key: str, value: str) -> None:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            conn.execute(
                "INSERT INTO gate_state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
            conn.commit()

    async def clear_gate_history(self, conv_id: str) -> int:
        """清空决策消息历史：`gate_messages` 全量删除 + 水位线清零，返回删除行数

        时间戳元信息 kv（last_bot_at / last_proactive_at）不属于上下文，保留；
        后续 append 从新行号继续（自增不回卷）。
        """
        return await asyncio.to_thread(self._clear_gate_sync, conv_id)

    def _clear_gate_sync(self, conv_id: str) -> int:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            deleted = conn.execute("DELETE FROM gate_messages").rowcount
            conn.execute("DELETE FROM gate_state WHERE key = 'watermark'")
            conn.commit()
        return deleted
