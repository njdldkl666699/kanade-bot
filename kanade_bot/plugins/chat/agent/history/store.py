"""会话历史存储：`ModelMessage` 全量持久化 + 压缩事件标记，每会话一库"""

import asyncio
import json
import math
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from nonebot import logger
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
        messages: list[ModelMessage] = []
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            # 游标迭代而非 fetchall：避免全量 payload 字节与对象图同时驻留
            rows = conn.execute("SELECT payload FROM messages ORDER BY seq")
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

    async def load_compaction_marks(
        self, conv_id: str, *, rehydrate_results: bool = True
    ) -> list[CompactionMark]:
        """按写入顺序返回该会话的压缩事件

        `rehydrate_results=False`：result 保持库内占位形态。restore 快路径用
        ——被支配 marks 的快照还原是纯浪费，只需还原真正参与重放的。
        """
        return await asyncio.to_thread(self._load_marks_sync, conv_id, rehydrate_results)

    def _load_marks_sync(self, conv_id: str, rehydrate_results: bool) -> list[CompactionMark]:
        db = self.registry.get(conv_id)
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT strategy, params, result, fingerprint, applied_at, up_to_seq "
                "FROM compaction_marks ORDER BY id"
            ).fetchall()

        marks: list[CompactionMark] = []
        for r in rows:
            result: bytes | None = r[2]
            if result is not None and rehydrate_results:
                messages = ModelMessagesTypeAdapter.validate_json(result)
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

        流程：**先剪枝再重放**（`prune_dominated_marks`）。被支配 marks
        不参与重放，也不参与漂移检测——漂移只关心真正会被应用的 marks，
        死 mark 的过期指纹不应触发丢弃健康摘要的重建（413 事故教训）。

        快路径（`_replay_pruned`）：首条幸存 mark 为摘要档时，其覆盖前缀
        的消息行完全不读取（重放直接采用存储快照）；clear 段与最终尾部
        按需实体化。行↔消息非 1:1 的旧数据自动回退保守全量重放。

        `params` 为当前压缩参数；与幸存 marks 指纹不一致（重放相关参数
        漂移）时，丢弃旧 marks 并按新参数从全量重放，重放有实际效果时
        落一条覆盖全量的新 mark，保证后续恢复状态稳定。

        结果缓存为占位形态：命中后只需还原 blobs（LRU 兜底），写路径
        自动失效缓存。
        """
        db = self.registry.get(conv_id)
        fingerprint = params.fingerprint() if params is not None else None
        if (cached := db.take_restore_cache(fingerprint)) is not None:
            return rehydrate_messages(cached, db.fetch_blob)

        marks = prune_dominated_marks(
            await self.load_compaction_marks(conv_id, rehydrate_results=False)
        )

        if not marks:
            final = await self.load(conv_id)
            if not final:
                return final
        elif params is not None and any(m.fingerprint != params.fingerprint() for m in marks):
            final = await self._restore_after_drift(conv_id, params)
        else:
            try:
                final = await self._replay_pruned(conv_id, marks)
            except _LegacyLayoutError as e:
                # 行↔消息 1:1 假设被破坏（旧数据防御）：回退保守全量重放
                logger.warning(f"会话{conv_id}restore 回退保守全量重放：{e}")
                final = await self._replay_full(conv_id)

        externalized, _ = await asyncio.to_thread(externalize_messages, final)
        db.put_restore_cache(fingerprint, externalized)
        return final

    async def _restore_after_drift(
        self, conv_id: str, params: CompactionConfig
    ) -> list[ModelMessage]:
        """漂移路径：丢弃旧 marks，全量真实形态按新参数从全量重放"""
        history = await self.load(conv_id)
        if not history:
            return history
        await self.clear_marks(conv_id)
        replayed = await apply_strategy(history, build_clear_mark(params))
        if not _messages_equal(replayed, history):
            mark = build_clear_mark(params)
            mark.up_to_seq = len(history) - 1
            await self.add_compaction_mark(conv_id, mark)
        return replayed

    async def _replay_full(self, conv_id: str) -> list[ModelMessage]:
        """保守全量重放：全量真实形态 + 未经剪枝的全部 marks（v2 原路径）"""
        history = await self.load(conv_id)
        if not history:
            return history
        marks = await self.load_compaction_marks(conv_id)
        acc: list[ModelMessage] = []
        covered = -1
        for mark in marks:
            boundary = mark.up_to_seq if mark.up_to_seq >= 0 else len(history) - 1
            acc = await apply_strategy([*acc, *history[covered + 1 : boundary + 1]], mark)
            covered = boundary
        return [*acc, *history[covered + 1 :]]

    async def _replay_pruned(self, conv_id: str, marks: list[CompactionMark]) -> list[ModelMessage]:
        """剪枝后 mark 链的快路径重放：跳过被支配前缀的读取与实体化

        首条幸存 mark 为摘要档（result 快照）时，其覆盖范围内的消息行
        不读取——重放直接采用快照，输入被整体替换。clear 段在真实形态
        上执行（token 决策依赖内容，见 SESSION_STORE_DESIGN §4 不变量），
        最终尾部按需实体化。
        """
        db = self.registry.get(conv_id)
        max_seq = await self.max_seq(conv_id)

        # -1 表示覆盖全部（旧数据兼容）：按行↔消息 1:1 折算为 max_seq
        effective = [max_seq if m.up_to_seq < 0 else m.up_to_seq for m in marks]
        if effective != sorted(effective):
            # 写路径只会追加非递减边界；非单调属异常数据，保守回退
            raise _LegacyLayoutError(f"mark 边界非单调：{effective}")

        # 首条幸存 mark 为摘要档：被其覆盖的前缀消息行无需读取
        cut = effective[0] if marks[0].result is not None else -1
        tail = await self._load_messages(conv_id, after_seq=cut)  # 占位形态
        base = cut + 1  # tail[0] 对应的全量消息下标
        end = max_seq + 1  # 全量消息条数（行↔消息 1:1）

        def seg(lo: int, hi: int) -> list[ModelMessage]:
            """全量 history[lo:hi] 映射到 tail 切片（占位形态）"""
            a = min(max(lo - base, 0), len(tail))
            b = min(max(hi - base, 0), len(tail))
            return tail[a:b]

        acc: list[ModelMessage] = []
        covered = cut
        for mark, boundary in zip(marks, effective):
            if mark.result is None:
                # 零成本档：token 决策依赖内容 → 喂真实形态
                segment = await asyncio.to_thread(
                    rehydrate_messages, seg(covered + 1, boundary + 1), db.fetch_blob
                )
                acc = await apply_strategy([*acc, *segment], mark)
            else:
                # 摘要档：直接采用还原后的快照，segment 输入被整体丢弃
                snapshot = ModelMessagesTypeAdapter.validate_json(mark.result)
                acc = await asyncio.to_thread(rehydrate_messages, snapshot, db.fetch_blob)
            covered = boundary
        remainder = await asyncio.to_thread(
            rehydrate_messages, seg(covered + 1, end), db.fetch_blob
        )
        return [*acc, *remainder]

    async def max_seq(self, conv_id: str) -> int:
        """会话内最大消息 seq（空表返回 -1）；行↔消息 1:1 时即消息条数-1"""
        db = self.registry.get(conv_id)
        return await asyncio.to_thread(self._max_seq_sync, db)

    def _max_seq_sync(self, db) -> int:
        db.ensure()
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            row = conn.execute("SELECT COALESCE(MAX(seq), -1) FROM messages").fetchone()
        return row[0] if row else -1

    async def _load_messages(self, conv_id: str, *, after_seq: int = -1) -> list[ModelMessage]:
        """按 seq 顺序加载消息行（占位形态，不还原 blobs）

        只读取 seq 大于 `after_seq` 的行（被支配前缀跳读）。每行必须恰好
        对应一条消息（seq ↔ 消息下标 1:1 是快路径重放切片的前提），否则
        抛 `_LegacyLayoutError` 由调用方回退保守路径。
        """
        db = self.registry.get(conv_id)
        return await asyncio.to_thread(self._load_messages_sync, db, after_seq)

    def _load_messages_sync(self, db, after_seq: int) -> list[ModelMessage]:
        db.ensure()
        messages: list[ModelMessage] = []
        with closing(sqlite3.connect(db.db_path, timeout=10)) as conn:
            rows = conn.execute(
                "SELECT payload FROM messages WHERE seq > ? ORDER BY seq", (after_seq,)
            )
            for (payload,) in rows:
                row_messages = ModelMessagesTypeAdapter.validate_json(payload)
                if len(row_messages) != 1:
                    raise _LegacyLayoutError(
                        f"存在一行多消息的旧布局行（{len(row_messages)} 条），无法按 seq 切片"
                    )
                messages.append(row_messages[0])
        return messages

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


class _LegacyLayoutError(RuntimeError):
    """快路径的行↔消息 1:1 / 边界单调假设被旧数据破坏

    restore 捕获后回退 v2 保守全量重放（`SessionStore._replay_full`）。
    """


def prune_dominated_marks(marks: Sequence[CompactionMark]) -> list[CompactionMark]:
    """剔除被支配的压缩 marks（纯读路径优化，不改库）

    边界为 B 的摘要档 mark（`result` 快照非空）支配它之前边界 ≤ B 的全部
    marks：其重放直接采用存储快照、完全忽略输入（见 `apply_strategy`），
    被支配 marks 的重放产物必然被整体替换，对最终结果零贡献。clear 档的
    输出参与后续策略决策，不支配任何 mark。

    `up_to_seq = -1` 表示覆盖全部，按 +∞ 参与支配判定。写路径只追加
    非递减边界，但剪枝不依赖这一点（防御异常数据）。
    """
    survivors: list[CompactionMark] = []
    dominant = -math.inf  # 右侧最近幸存摘要档的覆盖边界
    for mark in reversed(marks):
        boundary = math.inf if mark.up_to_seq < 0 else mark.up_to_seq
        if boundary <= dominant:
            continue  # 被右侧摘要档支配，重放产物必然被替换
        survivors.append(mark)
        if mark.result is not None:
            dominant = boundary
    survivors.reverse()
    return survivors
