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
    """生效行 `(id, role, content)`（按行号升序）"""

    total_rows: int
    """全量行数（含时间边界之前的行，统计观测用）"""


@dataclass
class GateStore:
    """决策历史存储：与主会话消息同库（经共享 `ConvDBRegistry` 路由）"""

    registry: ConvDBRegistry

    async def append_gate_messages(
        self, conv_id: str, entries: Sequence[tuple[str, str]]
    ) -> list[int]:
        """决策消息历史全量 append

        :param entries: `(role, content)` 列表；role 为 'user' 或 'assistant'
        :returns: 新写入的行 id 列表（与 entries 等长、顺序一致）
        """
        return await asyncio.to_thread(self._append_gate_sync, conv_id, list(entries))

    def _append_gate_sync(self, conv_id: str, entries: list[tuple[str, str]]) -> list[int]:
        db = self.registry.get(conv_id)
        db.ensure()
        new_rows: list[int] = []
        now = time.time()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            for role, content in entries:
                cursor = conn.execute(
                    "INSERT INTO gate_messages (kind, role, content, created_at) "
                    "VALUES ('msg', ?, ?, ?)",
                    (role, content, now),
                )
                new_rows.append(cursor.lastrowid or 0)
            conn.commit()
        return new_rows

    async def load_gate_history(
        self, conv_id: str, *, since: float | None = None
    ) -> GateHistorySnapshot:
        """加载决策消息历史的生效普通行（kind='msg'，按行号升序）

        历史版本的摘要锚点行（kind='anchor'）不再参与请求，直接忽略。

        :param since: 仅加载该时刻（含）之后的行；每日清空后重启仅恢复
            当日上下文用，None 为全量口径
        """
        return await asyncio.to_thread(self._load_gate_sync, conv_id, since)

    def _load_gate_sync(self, conv_id: str, since: float | None) -> GateHistorySnapshot:
        db = self.registry.get(conv_id)
        db.ensure()
        floor = since if since is not None else 0.0
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            total = conn.execute("SELECT COUNT(*) FROM gate_messages").fetchone()[0]

            msg_rows = conn.execute(
                "SELECT id, role, content FROM gate_messages "
                "WHERE kind = 'msg' AND created_at >= ? ORDER BY id",
                (floor,),
            ).fetchall()

        return GateHistorySnapshot(rows=list(msg_rows), total_rows=total)

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
        """写决策会话状态 kv（上次发言时间戳等，UPSERT 语义）"""
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
        """清空决策消息历史：`gate_messages` 全量删除，返回删除行数

        时间戳元信息 kv（last_bot_at / last_proactive_at）不属于上下文，保留；
        后续 append 从新行号继续（自增不回卷）。
        """
        return await asyncio.to_thread(self._clear_gate_sync, conv_id)

    def _clear_gate_sync(self, conv_id: str) -> int:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            deleted = conn.execute("DELETE FROM gate_messages").rowcount
            conn.commit()
        return deleted
