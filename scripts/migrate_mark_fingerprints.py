"""一次性迁移：把 compaction_marks.fingerprint 从旧全量形态改写为重放相关子集形态

背景：`CompactionConfig.fingerprint()` 已收窄为仅含重放相关字段
（trigger_fraction / keep_pairs / context_window）。存量 marks 的指纹列
仍是旧的全量 `model_dump_json()`，与运行时新形态永不相等——restore 的
漂移检查会误判，丢弃 marks（含摘要产物）并从全量重放。

本脚本把存量指纹就地改写为按各 mark 自身 params 计算的新形态。
幂等：已是新形态的 mark 不动，可安全重跑。

用法（先停 bot，再在仓库根目录运行）：
    python3 scripts/migrate_mark_fingerprints.py [--dry-run] [sessions_dir]

默认 sessions_dir 为 data/chat/sessions。仅改写 compaction_marks.fingerprint
列，messages / blobs 不动；每个有改写的库先备份为 *.pre-fingerprint-migration.bak。
"""

from __future__ import annotations

import argparse
import glob
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

REPLAY_FIELDS = ("trigger_fraction", "keep_pairs", "context_window")
"""新形态指纹包含的字段（顺序与 CompactionConfig 字段定义顺序一致）"""

FIELD_DEFAULTS: dict[str, object] = {
    "trigger_fraction": 0.7,
    "keep_pairs": 3,
    "context_window": None,
}
"""与 CompactionConfig 字段默认值保持一致（params 列缺失该键时兜底）"""

BACKUP_SUFFIX = ".pre-fingerprint-migration.bak"


def canonical_fingerprint(values: dict[str, object]) -> str:
    """按运行时 CompactionConfig.fingerprint() 的序列化格式生成新形态指纹

    pydantic `model_dump_json(include=...)`：紧凑分隔符、字段定义顺序。
    """
    obj = {name: values.get(name, FIELD_DEFAULTS[name]) for name in REPLAY_FIELDS}
    return json.dumps(obj, separators=(",", ":"))


def extract_values(mark_id: int, params_json: str | None, fingerprint: str) -> dict | None:
    """从 params 列（优先）或旧指纹 JSON 中取出字段值；两者都不可解析返回 None"""
    for source in (params_json, fingerprint):
        if not source:
            continue
        try:
            parsed = json.loads(source)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    print(f"  [错误] mark id={mark_id}: params 与指纹均不可解析，跳过（需人工处理）")
    return None


def migrate_db(db_path: Path, dry_run: bool) -> tuple[int, int, int]:
    """迁移单个会话库，返回 (总行数, 改写行数, 跳过行数)"""

    # 先吸收 WAL，保证随后备份的主库文件是完整快照
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with closing(sqlite3.connect(db_path)) as conn:
        try:
            rows = conn.execute(
                "SELECT id, params, fingerprint FROM compaction_marks ORDER BY id"
            ).fetchall()
        except sqlite3.OperationalError:
            # 无该表：不是会话库（或旧布局），跳过
            return 0, 0, 0

        rewritten: list[tuple[str, int]] = []
        for mark_id, params_json, fingerprint in rows:
            stored = fingerprint or ""
            values = extract_values(mark_id, params_json, stored)
            if values is None:
                continue
            canonical = canonical_fingerprint(values)
            if stored == canonical:
                continue  # 已是新形态
            rewritten.append((canonical, mark_id))

        if not rewritten:
            return len(rows), 0, len(rows)

        if not dry_run:
            backup = db_path.with_name(db_path.name + BACKUP_SUFFIX)
            if not backup.exists():
                shutil.copy2(db_path, backup)
            conn.executemany("UPDATE compaction_marks SET fingerprint = ? WHERE id = ?", rewritten)
            conn.commit()
        return len(rows), len(rewritten), len(rows) - len(rewritten)


def verify_db(db_path: Path) -> int:
    """返回仍为非新形态的行数（迁移后应为 0）"""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as conn:
        try:
            rows = conn.execute("SELECT id, params, fingerprint FROM compaction_marks").fetchall()
        except sqlite3.OperationalError:
            return 0
        stale = 0
        for mark_id, params_json, fingerprint in rows:
            values = extract_values(mark_id, params_json, fingerprint or "")
            if values is None or (fingerprint or "") != canonical_fingerprint(values):
                stale += 1
        return stale


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sessions_dir", nargs="?", default="data/chat/sessions")
    parser.add_argument("--dry-run", action="store_true", help="只报告，不写入")
    args = parser.parse_args()

    root = Path(args.sessions_dir)
    db_paths = sorted(Path(p) for p in glob.glob(str(root / "*.sqlite3")))
    if not db_paths:
        print(f"未找到会话库：{root.resolve()}")
        return 1

    total_db = total_rows = total_rewritten = 0
    for db_path in db_paths:
        rows, rewritten, skipped = migrate_db(db_path, args.dry_run)
        if rows == 0 and rewritten == 0 and skipped == 0:
            continue
        total_db += 1
        total_rows += rows
        total_rewritten += rewritten
        status = "改写" if rewritten else "无需改写"
        print(f"{db_path.name}: {rows} marks，{rewritten} {status}，{skipped} 跳过")

    action = "将改写" if args.dry_run else "已改写"
    print(f"\n共 {total_db} 个库 / {total_rows} marks：{total_rewritten} {action}")

    if not args.dry_run:
        stale = sum(verify_db(p) for p in db_paths)
        if stale:
            print(f"[错误] 校验仍有 {stale} 行非新形态，请检查上方错误信息")
            return 1
        print("校验通过：所有 marks 指纹均为新形态")
        print("提示：曾被漂移重放复活过历史的会话（上下文异常变大）仍需手动 /压缩 或 /重置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
