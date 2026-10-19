#!/usr/bin/env python3
"""会话数据库查看器（Session Store Viewer）

在网页上选取会话 SQLite 库（v2 三表结构：messages / compaction_marks / blobs），
服务器端用 ModelMessagesTypeAdapter 把 payload 转换为视图模型，FastAPI + Jinja2 渲染
对话历史：原始消息流 + 压缩事件时间线 + summarizing 产物快照 + 外置图片。

只读工具：以 mode=ro 打开数据库（WAL 恢复受限时回退普通连接，仅执行 SELECT），
不 import 项目内模块，blob 占位符格式与 message_blobs.BLOB_REF_PREFIX 保持一致。

用法：
    uv run -m scripts.session_viewer [--host 127.0.0.1] [--port 8770] [--no-browser]
"""

import argparse
import json
import re
import sqlite3
import threading
import time
import webbrowser
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from jinja2 import DictLoader, Environment
from pydantic_ai import (
    ModelMessagesTypeAdapter,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

ROOT = Path(__file__).resolve().parent.parent


# ===== 常量 =====

BLOB_REF_PREFIX = "$kanade-blob:"
"""与 kanade_bot/plugins/chat/agent/message_blobs.py 保持一致"""

_BLOB_REF_SCAN = re.compile(re.escape(BLOB_REF_PREFIX) + r"([0-9a-f]{64})")
_BLOB_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

SESSION_DIRS = [ROOT / "data" / "chat" / "sessions"]
"""首页自动扫描的会话库目录（--dir 可追加）"""

_LONG_TEXT = 1200
"""超过该字符数的文本渲染为可折叠长文块"""

# ===== 数据库访问（只读） =====


def open_ro(path: Path) -> sqlite3.Connection:
    """只读连接；WAL 库需要恢复而 ro 打开失败时回退普通连接（仍只执行 SELECT）"""
    uri = f"file:{quote(str(path))}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn
    except sqlite3.Error:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return conn


def has_session_schema(conn: sqlite3.Connection) -> bool:
    rows = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    return {"messages", "compaction_marks", "blobs"} <= rows


def probe_db(path: Path) -> dict | None:
    """候选库摘要；非 v2 结构或无法打开时返回 None"""
    try:
        conn = open_ro(path)
    except sqlite3.Error:
        return None
    try:
        if not has_session_schema(conn):
            return None
        rows, first, last, payload_bytes = conn.execute(
            "SELECT COUNT(*), MIN(created_at), MAX(created_at), "
            "COALESCE(SUM(LENGTH(payload)), 0) FROM messages"
        ).fetchone()
        marks = conn.execute("SELECT COUNT(*) FROM compaction_marks").fetchone()[0]
        blobs_count, blobs_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM blobs"
        ).fetchone()
        return {
            "path": path,
            "rows": rows or 0,
            "first_ts": first or 0.0,
            "last_ts": last or 0.0,
            "payload_bytes": payload_bytes or 0,
            "marks": marks or 0,
            "blobs_count": blobs_count or 0,
            "blobs_bytes": blobs_bytes or 0,
            "size": path.stat().st_size,
            "mtime": path.stat().st_mtime,
        }
    except sqlite3.Error:
        return None
    finally:
        conn.close()


# ===== ModelMessage → 视图模型 =====


def _content_items(content: object, blob_meta: dict[str, str], db_q: str) -> list[dict]:
    """part 内容（str 或含占位符的序列）→ 渲染项列表"""
    items: list[dict] = []

    def add_text(text: str) -> None:
        if not text:
            return
        items.append({"kind": "text", "text": text, "long": len(text) > _LONG_TEXT})

    def add_one(value: object) -> None:
        if isinstance(value, str):
            m = re.fullmatch(re.escape(BLOB_REF_PREFIX) + r"([0-9a-f]{64}):(.+)", value)
            if m:
                blob_hash, mime = m.group(1), m.group(2)
                items.append(
                    {
                        "kind": "image",
                        "hash": blob_hash,
                        "mime": mime,
                        "missing": blob_hash not in blob_meta,
                        "url": f"/blob?db={db_q}&hash={blob_hash}",
                    }
                )
            else:
                add_text(value)
        elif isinstance(value, (list, tuple)):
            for v in value:
                add_one(v)
        elif value is None:
            pass
        else:
            try:
                add_text(json.dumps(value, ensure_ascii=False, indent=2, default=str))
            except (TypeError, ValueError):
                add_text(str(value))

    add_one(content)
    return items


def _pretty_args(args: object) -> str:
    if isinstance(args, str):
        try:
            return json.dumps(json.loads(args), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            return args
    try:
        return json.dumps(args, ensure_ascii=False, indent=2, default=str)
    except (TypeError, ValueError):
        return str(args)


def _short_call_id(call_id: str | None) -> str:
    if not call_id:
        return ""
    return call_id if len(call_id) <= 14 else f"…{call_id[-12:]}"


def _usage_view(usage: object) -> dict:
    return {
        "input": getattr(usage, "input_tokens", 0) or 0,
        "cache": getattr(usage, "cache_read_tokens", 0) or 0,
        "output": getattr(usage, "output_tokens", 0) or 0,
    }


def convert_message(msg: object, blob_meta: dict[str, str], db_q: str) -> dict:
    """单条 ModelMessage → 视图模型"""
    view: dict = {"kind": getattr(msg, "kind", "?"), "parts": [], "usage": None}
    if isinstance(msg, ModelResponse):
        view["model"] = getattr(msg, "model_name", "") or ""
        view["finish"] = getattr(msg, "finish_reason", "") or ""
        view["usage"] = _usage_view(getattr(msg, "usage", None))

    instructions = getattr(msg, "instructions", None)
    if instructions:
        # 每轮 run 的首条 request 都会携带当时的 instructions（pydantic-ai 行为），
        # 量级可达数万字；展示层按内容去重，仅首次出现时渲染（见 _dedup_instructions）
        view["parts"].append(
            {"type": "instructions", "content": str(instructions), "chars": len(str(instructions))}
        )

    for part in getattr(msg, "parts", []):
        if isinstance(part, UserPromptPart):
            view["parts"].append(
                {"type": "user", "blocks": _content_items(part.content, blob_meta, db_q)}
            )
        elif isinstance(part, SystemPromptPart):
            view["parts"].append(
                {"type": "system", "content": str(part.content), "label": "system"}
            )
        elif isinstance(part, ToolReturnPart):
            view["parts"].append(
                {
                    "type": "tool_return",
                    "tool": part.tool_name,
                    "call_id": _short_call_id(part.tool_call_id),
                    "blocks": _content_items(part.content, blob_meta, db_q),
                }
            )
        elif isinstance(part, RetryPromptPart):
            view["parts"].append(
                {
                    "type": "retry",
                    "tool": part.tool_name or "",
                    "call_id": _short_call_id(part.tool_call_id),
                    "blocks": _content_items(part.content, blob_meta, db_q),
                }
            )
        elif isinstance(part, TextPart):
            view["parts"].append(
                {"type": "text", "blocks": _content_items(part.content, blob_meta, db_q)}
            )
        elif isinstance(part, ThinkingPart):
            view["parts"].append(
                {
                    "type": "thinking",
                    "blocks": _content_items(part.content, blob_meta, db_q),
                    "chars": len(part.content or ""),
                }
            )
        elif isinstance(part, ToolCallPart):
            view["parts"].append(
                {
                    "type": "tool_call",
                    "tool": part.tool_name,
                    "call_id": _short_call_id(part.tool_call_id),
                    "args": _pretty_args(part.args),
                }
            )
        else:
            view["parts"].append({"type": "unknown", "name": type(part).__name__})
    return view


def convert_payload(raw: bytes, blob_meta: dict[str, str], db_q: str) -> dict:
    """一行 payload（或 mark result）→ {'messages': [...]}；解析失败时降级"""
    try:
        messages = ModelMessagesTypeAdapter.validate_json(raw)
    except Exception as e:
        return {
            "ok": False,
            "error": f"{type(e).__name__}: {e}",
            "raw_head": raw[:4000].decode("utf-8", "replace"),
        }
    return {"ok": True, "messages": [convert_message(m, blob_meta, db_q) for m in messages]}


def collect_blob_hashes(*blobs: bytes) -> set[str]:
    """从原始 payload 文本中扫出全部占位 hash"""
    found: set[str] = set()
    for raw in blobs:
        if raw:
            found.update(_BLOB_REF_SCAN.findall(raw.decode("utf-8", "replace")))
    return found


def load_blob_meta(conn: sqlite3.Connection, hashes: set[str]) -> dict[str, str]:
    if not hashes:
        return {}
    out: dict[str, str] = {}
    for chunk_start in range(0, len(hashes), 500):
        chunk = sorted(hashes)[chunk_start : chunk_start + 500]
        placeholders = ",".join("?" * len(chunk))
        for h, mime in conn.execute(
            f"SELECT hash, mime FROM blobs WHERE hash IN ({placeholders})", chunk
        ):
            out[h] = mime
    return out


# ===== 页面数据构建 =====


def ts_fmt(ts: float | None) -> str:
    if not ts:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))


def human_num(value: float) -> str:
    n = float(value)
    for unit in ("", "k", "M", "G"):
        if abs(n) < 1024 if unit else n < 1000:
            return f"{n:,.0f}{unit}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def page_window(cur: int, total: int, span: int = 2) -> list[int | None]:
    """页码窗口，None 表示省略号"""
    if total <= 0:
        return [1]
    pages = {1, total, *range(max(1, cur - span), min(total, cur + span) + 1)}
    out: list[int | None] = []
    prev = 0
    for p in sorted(pages):
        if prev and p - prev > 1:
            out.append(None)
        out.append(p)
        prev = p
    return out


def scan_instructions(conn: sqlite3.Connection) -> dict[str, dict]:
    """全量扫描 request.instructions，产出按首次出现顺序编号的版本表

    :return: {instructions 文本: {"ver": 版本号, "count": 全会话出现次数}}
    """
    versions: dict[str, dict] = {}
    for (payload,) in conn.execute("SELECT payload FROM messages ORDER BY seq"):
        for m in json.loads(payload):
            if m.get("kind") == "request" and m.get("instructions") is not None:
                content = m["instructions"]
                if content not in versions:
                    versions[content] = {"ver": len(versions) + 1, "count": 0}
                versions[content]["count"] += 1
    return versions


def _dedup_instructions(converted: dict, seen: set[str], versions: dict[str, dict]) -> None:
    """从已转换消息里移除重复的 instructions part，首见的附上版本信息"""
    if not converted.get("ok"):
        return
    for m in converted["messages"]:
        kept: list[dict] = []
        for p in m["parts"]:
            if p["type"] == "instructions":
                if p["content"] in seen:
                    continue
                seen.add(p["content"])
                meta = versions.get(p["content"])
                if meta:
                    p["ver"] = meta["ver"]
                    p["count"] = meta["count"]
                    p["total"] = len(versions)
            kept.append(p)
        m["parts"] = kept


def build_view_data(db: Path, page: int, page_size: int) -> dict:
    conn = open_ro(db)
    try:
        total_rows = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        total_pages = max(1, -(-total_rows // page_size))
        if page <= 0:  # 0 / 负数 = 跳到最新一页
            page = total_rows
        page = min(max(1, page), total_pages)
        rows = conn.execute(
            "SELECT seq, payload, created_at FROM messages ORDER BY seq LIMIT ? OFFSET ?",
            (page_size, (page - 1) * page_size),
        ).fetchall()

        marks_raw = conn.execute(
            "SELECT id, strategy, params, result, fingerprint, applied_at, up_to_seq "
            "FROM compaction_marks ORDER BY id"
        ).fetchall()

        # 先扫占位 hash，再批量取 mime；同时全量扫 instructions 版本表（去重展示用）
        hashes = collect_blob_hashes(*[r[1] for r in rows], *[r[3] for r in marks_raw if r[3]])
        blob_meta = load_blob_meta(conn, hashes)
        instr_versions = scan_instructions(conn)
    finally:
        conn.close()

    db_q = quote(str(db), safe="")

    # ---- 行视图 ----
    part_counter: Counter[str] = Counter()
    msg_count = 0
    row_views = []
    seen_instructions: set[str] = set()  # 页内去重（含 marks 快照），每版本仅首见渲染
    for seq, payload, created_at in rows:
        converted = convert_payload(payload, blob_meta, db_q)
        _dedup_instructions(converted, seen_instructions, instr_versions)
        if converted["ok"]:
            msg_count += len(converted["messages"])
            for m in converted["messages"]:
                for p in m["parts"]:
                    part_counter[p["type"]] += 1
        row_views.append({"seq": seq, "ts": ts_fmt(created_at), "payload": converted, "covered": 0})

    # ---- 压缩事件视图 ----
    mark_views = []
    for mid, strategy, params, result, fingerprint, applied_at, up_to_seq in marks_raw:
        mv: dict = {
            "id": mid,
            "strategy": strategy,
            "up_to_seq": up_to_seq,
            "applied_dt": ts_fmt(applied_at),
            "fingerprint": (fingerprint or "")[:8],
            "params_pretty": json.dumps(json.loads(params), ensure_ascii=False, indent=2),
            "has_result": result is not None,
            "result_count": 0,
            "result_messages": None,
        }
        if result is not None:
            converted = convert_payload(result, blob_meta, db_q)
            _dedup_instructions(converted, seen_instructions, instr_versions)
            if converted["ok"]:
                mv["result_count"] = len(converted["messages"])
                mv["result_messages"] = converted["messages"]
            else:
                mv["result_count"] = -1
                mv["result_messages"] = converted
        mark_views.append(mv)

    # 覆盖标记与边界横幅
    banners: dict[int, list[dict]] = {}
    for mv in mark_views:
        boundary = mv["up_to_seq"]
        for rv in row_views:
            if boundary < 0 or rv["seq"] <= boundary:
                rv["covered"] += 1
        banners.setdefault(boundary, []).append(mv)

    first_ts = row_views[0]["ts"] if row_views else "-"
    last_ts = row_views[-1]["ts"] if row_views else "-"

    blobs_count, blobs_bytes = 0, 0
    conn = open_ro(db)
    try:
        blobs_count, blobs_bytes = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(data)), 0) FROM blobs"
        ).fetchone()
    finally:
        conn.close()

    return {
        "db": str(db),
        "db_name": db.name,
        "db_q": db_q,
        "page": page,
        "total_pages": total_pages,
        "pages": page_window(page, total_pages),
        "total_rows": total_rows,
        "msg_count": msg_count,
        "page_rows": len(row_views),
        "part_stats": dict(part_counter.most_common()),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "rows": row_views,
        "marks": mark_views,
        "banners": banners,
        "instr_versions": len(instr_versions),
        "instr_total": sum(v["count"] for v in instr_versions.values()),
        "blobs_count": blobs_count,
        "blobs_bytes": blobs_bytes,
    }


def build_index_data(extra_dirs: list[Path]) -> dict:
    seen: set[Path] = set()
    candidates: list[Path] = []
    for d in [*SESSION_DIRS, *extra_dirs]:
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.sqlite3")):
            rp = p.resolve()
            if rp not in seen:
                seen.add(rp)
                candidates.append(rp)

    entries = []
    for p in candidates:
        info = probe_db(p)
        if info:
            entries.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "dir": str(p.parent),
                    "url": f"/view?db={quote(str(p), safe='')}",
                    "rows": info["rows"],
                    "marks": info["marks"],
                    "blobs_count": info["blobs_count"],
                    "size": info["size"],
                    "mtime": ts_fmt(info["mtime"]),
                    "last_ts": ts_fmt(info["last_ts"]),
                    "payload_bytes": info["payload_bytes"],
                }
            )
    entries.sort(key=lambda e: (-e["rows"], e["name"]))
    return {"entries": entries}


# ===== 模板 =====

TEMPLATE = r"""<!doctype html>
<html lang="zh-cn">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{% block title %}kanade 会话查看器{% endblock %}</title>
<style>
  :root {
    --bg: #0e1116; --panel: #151a23; --panel2: #1a2130; --border: #262f42;
    --text: #c9d4e5; --dim: #7d8aa0; --accent: #6ea8fe; --green: #3ddc97;
    --purple: #b48cff; --red: #ff7b72; --yellow: #e3b341; --mono: ui-monospace, "Cascadia Mono", Consolas, monospace;
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--text);
         font: 13px/1.65 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header { position: sticky; top: 0; z-index: 9; background: rgba(14,17,22,.92);
           backdrop-filter: blur(6px); border-bottom: 1px solid var(--border);
           padding: 10px 18px; display: flex; flex-wrap: wrap; gap: 8px 16px; align-items: baseline; }
  header h1 { font-size: 15px; margin: 0; color: #e8eefb; }
  header .path { font-family: var(--mono); font-size: 11px; color: var(--dim); word-break: break-all; }
  main { max-width: 1080px; margin: 0 auto; padding: 16px 18px 60px; }

  .chips { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
  .chip { background: var(--panel); border: 1px solid var(--border); border-radius: 999px;
          padding: 2px 10px; font-size: 11px; color: var(--dim); }
  .chip b { color: var(--text); font-weight: 600; }

  /* 首页 */
  table.list { width: 100%; border-collapse: collapse; font-size: 12.5px; }
  table.list th, table.list td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
  table.list th { color: var(--dim); font-weight: 500; font-size: 11.5px; }
  table.list td.num, table.list th.num { text-align: right; font-family: var(--mono); }
  .dir-note { font-size: 11px; color: var(--dim); font-family: var(--mono); }
  form.inline { display: flex; gap: 8px; margin: 14px 0; }
  form.inline input { flex: 1; background: var(--panel); border: 1px solid var(--border);
                      border-radius: 8px; padding: 7px 12px; color: var(--text); font-family: var(--mono); }
  form.inline button { background: var(--panel2); color: var(--accent); border: 1px solid var(--border);
                       border-radius: 8px; padding: 7px 18px; cursor: pointer; }

  /* 消息流 */
  .row { margin: 14px 0 22px; }
  .row-head { font-size: 11px; color: var(--dim); font-family: var(--mono);
              display: flex; gap: 10px; align-items: center; margin-bottom: 4px; }
  .row-head .cov { color: var(--purple); }
  .msg { border: 1px solid var(--border); border-left-width: 3px; border-radius: 10px;
         background: var(--panel); margin: 6px 0; overflow: hidden; }
  .msg.request { border-left-color: var(--accent); }
  .msg.response { border-left-color: var(--green); }
  .msg .kindline { display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
                   padding: 5px 12px 0; font-size: 10.5px; color: var(--dim); font-family: var(--mono); }
  .msg .body { padding: 6px 12px 10px; }
  .badge { display: inline-block; border-radius: 5px; padding: 0 6px; font-size: 10.5px;
           font-family: var(--mono); line-height: 1.7; }
  .b-user { background: #1d3a2f; color: var(--green); }
  .b-sys { background: #33384a; color: #aab4cc; }
  .b-call { background: #1c2f52; color: var(--accent); }
  .b-ret { background: #173a35; color: #57d9b0; }
  .b-retry { background: #4a2325; color: var(--red); }
  .b-think { background: #37304a; color: var(--purple); }

  .ctext { white-space: pre-wrap; word-break: break-word; font-size: 12.8px; }
  .clamp-wrap .ctext { max-height: 260px; overflow: hidden; position: relative;
    -webkit-mask-image: linear-gradient(180deg, #000 82%, transparent);
            mask-image: linear-gradient(180deg, #000 82%, transparent); }
  .clamp-wrap.open .ctext { max-height: none; -webkit-mask-image: none; mask-image: none; }
  .clamp-btn { background: none; border: 1px solid var(--border); color: var(--dim);
               border-radius: 6px; font-size: 10.5px; padding: 1px 10px; margin-top: 4px; cursor: pointer; }
  .clamp-btn::after { content: "展开全文 ▾"; }
  .clamp-wrap.open .clamp-btn::after { content: "收起 ▴"; }
  .clamp-wrap.open .clamp-btn { position: sticky; bottom: 8px; opacity: .92; }

  .part { margin: 6px 0; }
  .part.tool_call, .part.tool_return, .part.retry, .part.system {
    background: #10141c; border: 1px solid var(--border); border-radius: 8px; padding: 8px 10px; }
  .part pre { margin: 6px 0 0; font-family: var(--mono); font-size: 11.5px; color: #9fb0c8;
              white-space: pre-wrap; word-break: break-word; max-height: 300px; overflow: auto; }
  .part .toolhead { font-family: var(--mono); font-size: 11.5px; display: flex; gap: 8px;
                    align-items: center; flex-wrap: wrap; }
  .part .callid { color: var(--dim); font-size: 10.5px; }

  img.blob { max-width: 340px; max-height: 280px; border-radius: 8px; border: 1px solid var(--border);
             display: block; margin: 6px 0; }
  .blob-missing { display: inline-block; background: #4a3a20; color: var(--yellow); border-radius: 6px;
                  padding: 1px 8px; font-size: 11px; font-family: var(--mono); margin: 4px 0; }

  details.think { margin: 4px 0; }
  details.think > summary { cursor: pointer; color: var(--purple); font-size: 11.5px;
                            font-family: var(--mono); user-select: none; }
  details.think .ctext { color: #97a3ba; font-size: 12px; margin-top: 4px; }

  .meta { border-top: 1px dashed var(--border); margin-top: 6px; padding: 5px 12px 8px;
          font-family: var(--mono); font-size: 10.5px; color: var(--dim); display: flex;
          gap: 14px; flex-wrap: wrap; }
  .meta .model { color: var(--yellow); }

  .parse-error { border: 1px solid #5a2b2b; background: #241417; border-radius: 8px; padding: 8px 12px; }
  .parse-error pre { color: var(--red); font-size: 11px; white-space: pre-wrap; word-break: break-word;
                     max-height: 200px; overflow: auto; font-family: var(--mono); }

  /* 压缩事件 */
  .banner { border: 1px dashed #55407a; background: #191327; border-radius: 8px;
            padding: 6px 14px; margin: 10px 0; color: var(--purple); font-size: 12px;
            font-family: var(--mono); }
  .banner .sub { color: var(--dim); }
  details.marks { border: 1px solid var(--border); border-radius: 10px; background: var(--panel);
                  margin: 12px 0; }
  details.marks > summary { padding: 9px 14px; cursor: pointer; font-size: 12.5px; }
  details.marks > summary .sub { color: var(--dim); font-size: 11.5px; margin-left: 8px; }
  .mark-item { border-top: 1px solid var(--border); padding: 8px 14px; }
  .mark-item > summary { cursor: pointer; font-family: var(--mono); font-size: 12px; }
  .mark-item .grid { display: grid; grid-template-columns: 200px 1fr; gap: 10px; margin-top: 8px; }
  .mark-item pre.params { background: #10141c; border: 1px solid var(--border); border-radius: 8px;
                          padding: 8px 10px; font-size: 11px; color: #9fb0c8; margin: 0;
                          white-space: pre-wrap; font-family: var(--mono); }
  .kv { font-size: 11px; color: var(--dim); font-family: var(--mono); line-height: 2; }
  .kv b { color: var(--text); font-weight: 500; }

  /* 分页 */
  .pager { display: flex; gap: 6px; align-items: center; justify-content: center;
           flex-wrap: wrap; margin: 18px 0; font-family: var(--mono); font-size: 12px; }
  .pager a, .pager span.cur, .pager span.gap { padding: 3px 10px; border-radius: 7px;
      border: 1px solid var(--border); background: var(--panel); }
  .pager span.cur { color: var(--accent); border-color: var(--accent); }
  .pager span.gap { border: none; background: none; color: var(--dim); }

  .empty { text-align: center; color: var(--dim); padding: 60px 0; }
  .err { border: 1px solid #5a2b2b; background: #241417; border-radius: 10px;
         padding: 18px 22px; margin: 30px 0; }
  .err h2 { color: var(--red); font-size: 14px; margin: 0 0 8px; }
</style>
</head>
<body>
<header>
  <h1>📦 kanade 会话查看器</h1>
  {% if db_name is defined %}
    <span class="path">{{ db_name }}</span>
    <span class="path" style="opacity:.7">{{ db }}</span>
    <a href="/">← 切换数据库</a>
  {% else %}
    <span class="path">Session Store v2 · 只读</span>
  {% endif %}
</header>
<main>
{% block content %}{% endblock %}
</main>

{% block scripts %}
<script>
/* 长文本展开/收起仅由按钮触发，文本区域保持可选可复制 */
function clampBtn(el) { el.parentNode.classList.toggle('open'); return false; }
</script>
{% endblock %}
</body>
</html>
"""

PAGE_INDEX = r"""{% extends "base" %}
{% block content %}
  <h2 style="font-size:14px">选择会话数据库</h2>
  <p style="color:var(--dim);font-size:12px">
    自动扫描 <span class="dir-note">{{ scan_dirs }}</span> 下的 v2 结构库（messages / compaction_marks / blobs）。
  </p>
  <form class="inline" method="get" action="/view">
    <input name="db" placeholder="或输入任意 sqlite3 路径（相对项目根或绝对路径）" required>
    <button type="submit">打开</button>
  </form>
  {% if entries %}
  <table class="list">
    <tr><th>文件</th><th class="num">消息行</th><th class="num">压缩事件</th>
        <th class="num">blobs</th><th class="num">payload</th><th class="num">文件大小</th>
        <th>最后活动</th><th>修改时间</th></tr>
    {% for e in entries %}
    <tr>
      <td>
        <a href="{{ e.url }}"><b>{{ e.name }}</b></a>
        <div class="dir-note">{{ e.dir }}</div>
      </td>
      <td class="num">{{ e.rows }}</td>
      <td class="num">{{ e.marks }}</td>
      <td class="num">{{ e.blobs_count }}</td>
      <td class="num">{{ e.payload_bytes | human }}</td>
      <td class="num">{{ e.size | human }}</td>
      <td class="num" style="font-family:inherit">{{ e.last_ts }}</td>
      <td class="num" style="font-family:inherit">{{ e.mtime }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <div class="empty">扫描目录下没有找到 v2 结构的会话库</div>
  {% endif %}
{% endblock %}
"""

PAGE_VIEW = r"""{% extends "base" %}
{% block title %}{{ db_name }} · kanade 会话查看器{% endblock %}
{% macro content_block(blocks) %}
  {% for it in blocks %}
    {% if it.kind == 'text' %}
      {% if it.long %}
      <div class="clamp-wrap">
        <div class="ctext">{{ it.text }}</div>
        <button class="clamp-btn" onclick="return clampBtn(this)"></button>
      </div>
      {% else %}
      <div class="ctext">{{ it.text }}</div>
      {% endif %}
    {% elif it.kind == 'image' %}
      {% if it.missing %}
        <span class="blob-missing">⚠ blob 缺失 · {{ it.hash[:12] }} · {{ it.mime }}</span>
      {% else %}
        <a href="{{ it.url }}" target="_blank"><img class="blob" loading="lazy" src="{{ it.url }}"
            alt="{{ it.hash[:12] }}"></a>
      {% endif %}
    {% endif %}
  {% endfor %}
{% endmacro %}

{% macro message_card(m) %}
<div class="msg {{ m.kind }}">
  <div class="kindline">
    <span>{% if m.kind == 'request' %}▶ request{% elif m.kind == 'response' %}◀ response{% else %}{{ m.kind }}{% endif %}</span>
    {% if m.usage %}<span>{{ '{:,}'.format(m.usage.input) }} in · {{ '{:,}'.format(m.usage.cache) }} cache · {{ '{:,}'.format(m.usage.output) }} out</span>{% endif %}
    {% if m.model %}<span class="model">{{ m.model }}</span>{% endif %}
    {% if m.finish %}<span>{{ m.finish }}</span>{% endif %}
  </div>
  <div class="body">
  {% for p in m.parts %}
    {% if p.type == 'user' %}
      <div class="part"><span class="badge b-user">用户</span>
        <div style="margin-top:4px">{{ content_block(p.blocks) }}</div></div>
    {% elif p.type == 'text' %}
      <div class="part">{{ content_block(p.blocks) }}</div>
    {% elif p.type == 'system' %}
      <div class="part system"><span class="badge b-sys">{{ p.label }}</span>
        <div class="ctext" style="color:#97a3ba;font-size:12px;margin-top:4px">{{ p.content }}</div></div>
    {% elif p.type == 'instructions' %}
      <details class="think">
        <summary>📋 instructions{% if p.ver %} v{{ p.ver }}/{{ p.total }} · 全会话 {{ p.count }} 次（重复已折叠）{% endif %} · {{ '{:,}'.format(p.chars) }} 字符</summary>
        <div class="ctext">{{ p.content }}</div>
      </details>
    {% elif p.type == 'tool_call' %}
      <div class="part tool_call">
        <div class="toolhead"><span class="badge b-call">调用</span> <b>{{ p.tool }}</b>
          <span class="callid">{{ p.call_id }}</span></div>
        <pre>{{ p.args }}</pre>
      </div>
    {% elif p.type == 'tool_return' %}
      <div class="part tool_return">
        <div class="toolhead"><span class="badge b-ret">返回</span> <b>{{ p.tool }}</b>
          <span class="callid">{{ p.call_id }}</span></div>
        <div style="margin-top:2px">{{ content_block(p.blocks) }}</div>
      </div>
    {% elif p.type == 'retry' %}
      <div class="part retry">
        <div class="toolhead"><span class="badge b-retry">重试</span> <b>{{ p.tool }}</b>
          <span class="callid">{{ p.call_id }}</span></div>
        <div style="margin-top:2px">{{ content_block(p.blocks) }}</div>
      </div>
    {% elif p.type == 'thinking' %}
      <details class="think">
        <summary>💭 thinking · {{ p.chars }} 字符</summary>
        {{ content_block(p.blocks) }}
      </details>
    {% else %}
      <div class="part"><span class="badge b-sys">{{ p.name }}</span></div>
    {% endif %}
  {% endfor %}
  </div>
</div>
{% endmacro %}

{% block content %}
  <div class="chips">
    <span class="chip">行 <b>{{ '{:,}'.format(total_rows) }}</b></span>
    <span class="chip">本页消息 <b>{{ msg_count }}</b></span>
    <span class="chip">压缩事件 <b>{{ marks | length }}</b></span>
    <span class="chip">blobs <b>{{ blobs_count }}</b> · {{ blobs_bytes | human }}</span>
    {% if instr_total %}<span class="chip">instructions <b>{{ instr_total }}</b> 条 · {{ instr_versions }} 版本（去重展示）</span>{% endif %}
    {% for k, v in part_stats.items() %}
      <span class="chip">{{ k }} <b>{{ v }}</b></span>
    {% endfor %}
    <span class="chip">首条 {{ first_ts }}</span>
    <span class="chip">末条 {{ last_ts }}</span>
  </div>

  {% if marks %}
  <details class="marks">
    <summary>⚙ 压缩事件时间线（{{ marks | length }}）
      <span class="sub">{{ marks | selectattr('strategy', 'equalto', 'summarizing') | list | length }} 条摘要 ·
      {{ marks | selectattr('strategy', 'equalto', 'clear_tool_results') | list | length }} 条清理</span>
    </summary>
    {% for mk in marks %}
    <details class="mark-item">
      <summary>#{{ mk.id }} {{ mk.strategy }} → 覆盖至 seq {{ mk.up_to_seq }} · {{ mk.applied_dt }}
        {% if mk.has_result %}<span class="sub" style="color:var(--dim)">（产物 {{ mk.result_count }} 条）</span>{% endif %}
      </summary>
      <div class="grid">
        <div class="kv">
          <div>strategy <b>{{ mk.strategy }}</b></div>
          <div>up_to_seq <b>{{ mk.up_to_seq }}</b></div>
          <div>applied_at <b>{{ mk.applied_dt }}</b></div>
          <div>fingerprint <b>{{ mk.fingerprint }}</b></div>
          {% if mk.has_result %}<div>result <b>{{ mk.result_count }} 条消息</b></div>{% endif %}
        </div>
        <div><pre class="params">{{ mk.params_pretty }}</pre></div>
      </div>
      {% if mk.result_messages %}
        <div style="margin-top:10px">
          <div style="font-size:11px;color:var(--dim);margin-bottom:4px">压缩产物快照（summarizing 恢复时直接使用）：</div>
          {% for m in mk.result_messages %}{{ message_card(m) }}{% endfor %}
        </div>
      {% elif mk.has_result %}
        <div class="parse-error" style="margin-top:8px">产物解析失败：{{ mk.result_messages.error }}
          <pre>{{ mk.result_messages.raw_head }}</pre></div>
      {% endif %}
    </details>
    {% endfor %}
  </details>
  {% endif %}

  {% if total_pages > 1 %}
  <div class="pager">
    <a href="/view?db={{ db_q }}&page=1">« 最早</a>
    {% if page > 1 %}<a href="/view?db={{ db_q }}&page={{ page - 1 }}">‹ 上一页</a>{% endif %}
    {% for p in pages %}
      {% if p is none %}<span class="gap">…</span>
      {% elif p == page %}<span class="cur">{{ p }} / {{ total_pages }}</span>
      {% else %}<a href="/view?db={{ db_q }}&page={{ p }}">{{ p }}</a>{% endif %}
    {% endfor %}
    {% if page < total_pages %}<a href="/view?db={{ db_q }}&page={{ page + 1 }}">下一页 ›</a>{% endif %}
    <a href="/view?db={{ db_q }}&page={{ total_pages }}">最新 »</a>
  </div>
  {% endif %}

  {% for rv in rows %}
  <div class="row">
    <div class="row-head">
      <span>seq {{ rv.seq }}</span><span>{{ rv.ts }}</span>
      {% if rv.covered %}<span class="cov">⚙ 已被 {{ rv.covered }} 条压缩事件覆盖</span>{% endif %}
    </div>
    {% if rv.payload.ok %}
      {% for m in rv.payload.messages %}{{ message_card(m) }}{% endfor %}
    {% else %}
      <div class="parse-error">⚠ payload 解析失败：{{ rv.payload.error }}
        <pre>{{ rv.payload.raw_head }}</pre></div>
    {% endif %}
    {% for mk in banners.get(rv.seq, []) %}
    <div class="banner">⚙ 压缩事件 #{{ mk.id }} · {{ mk.strategy }}
      <span class="sub">覆盖至 seq {{ mk.up_to_seq }}（含）· 恢复时将
      {% if mk.strategy == 'clear_tool_results' %}按参数重放清理{% else %}使用以下快照替换{% endif %}</span>
    </div>
    {% endfor %}
  </div>
  {% else %}
  <div class="empty">该数据库没有消息</div>
  {% endfor %}

  {% if total_pages > 1 %}
  <div class="pager">
    <a href="/view?db={{ db_q }}&page=1">« 最早</a>
    {% if page > 1 %}<a href="/view?db={{ db_q }}&page={{ page - 1 }}">‹ 上一页</a>{% endif %}
    {% for p in pages %}
      {% if p is none %}<span class="gap">…</span>
      {% elif p == page %}<span class="cur">{{ p }} / {{ total_pages }}</span>
      {% else %}<a href="/view?db={{ db_q }}&page={{ p }}">{{ p }}</a>{% endif %}
    {% endfor %}
    {% if page < total_pages %}<a href="/view?db={{ db_q }}&page={{ page + 1 }}">下一页 ›</a>{% endif %}
    <a href="/view?db={{ db_q }}&page={{ total_pages }}">最新 »</a>
  </div>
  {% endif %}
{% endblock %}
"""

PAGE_ERROR = r"""{% extends "base" %}
{% block content %}
  <div class="err">
    <h2>{{ title }}</h2>
    <div style="font-family:var(--mono);font-size:12px;color:var(--dim);word-break:break-all">{{ detail }}</div>
  </div>
  <a href="/">← 返回数据库列表</a>
{% endblock %}
"""


def build_renderer() -> Environment:
    env = Environment(
        autoescape=True,
        loader=DictLoader(
            {"base": TEMPLATE, "index": PAGE_INDEX, "view": PAGE_VIEW, "error": PAGE_ERROR}
        ),
    )
    env.filters["human"] = human_num
    env.filters["dt"] = ts_fmt
    return env


# ===== HTTP 服务 =====

RENDER_ENV: Environment = build_renderer()

EXTRA_DIRS: list[Path] = []
PAGE_SIZE = 60


def render_page(name: str, **ctx) -> str:
    return RENDER_ENV.get_template(name).render(**ctx)


app = FastAPI(title="kanade 会话查看器", docs_url=None, redoc_url=None, openapi_url=None)


def _error_page(title: str, detail: str, status: int) -> HTMLResponse:
    return HTMLResponse(render_page("error", title=title, detail=detail), status_code=status)


def _resolve_db(raw: str) -> Path | None:
    """db 查询参数 → 绝对路径；空参数返回 None"""
    if not raw.strip():
        return None
    db = Path(raw).expanduser()
    if not db.is_absolute():
        db = Path.cwd() / db
    return db.resolve()


@app.exception_handler(sqlite3.Error)
@app.exception_handler(OSError)
async def _db_error(request: Request, exc: Exception) -> HTMLResponse:
    return _error_page("读取数据库失败", f"{type(exc).__name__}: {exc}", 500)


@app.get("/")
def index() -> HTMLResponse:
    data = build_index_data(EXTRA_DIRS)
    scan_note = "、".join(str(d) for d in [*SESSION_DIRS, *EXTRA_DIRS])
    return HTMLResponse(render_page("index", scan_dirs=scan_note, **data))


@app.get("/view")
def view(db: str = "", page: str = "0") -> HTMLResponse:
    path = _resolve_db(db)
    if path is None:
        return _error_page("缺少 db 参数", "请从首页选择数据库，或以 /view?db=<路径> 访问", 400)
    if not path.is_file():
        return _error_page("文件不存在", str(path), 404)
    conn = open_ro(path)
    try:
        ok_schema = has_session_schema(conn)
    finally:
        conn.close()
    if not ok_schema:
        return _error_page(
            "不是 v2 会话库", f"{path} 缺少 messages / compaction_marks / blobs 三表", 400
        )
    try:
        page_no = int(page)
    except ValueError:
        page_no = 0
    data = build_view_data(path, page_no, PAGE_SIZE)  # page<=0 时跳最新一页
    return HTMLResponse(render_page("view", **data))


@app.get("/blob")
def blob(db: str = "", hash: str = "") -> Response:
    if not _BLOB_HASH_RE.match(hash):
        return _error_page("参数无效", "需要 db 与 64 位 hex hash", 400)
    path = _resolve_db(db)
    if path is None or not path.is_file():
        return _error_page("文件不存在", str(path or db), 404)
    conn = open_ro(path)
    try:
        row = conn.execute("SELECT mime, data FROM blobs WHERE hash = ?", (hash,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return _error_page("blob 不存在", hash, 404)
    mime, data = row
    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


def main():
    global EXTRA_DIRS, PAGE_SIZE
    parser = argparse.ArgumentParser(description="kanade 会话数据库查看器（只读）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8770, help="监听端口（默认 8770）")
    parser.add_argument("--page-size", type=int, default=60, help="每页消息行数（默认 60）")
    parser.add_argument(
        "--dir", action="append", default=[], type=Path, help="追加扫描目录（可多次）"
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    EXTRA_DIRS = [d if d.is_absolute() else (ROOT / d).resolve() for d in args.dir]
    PAGE_SIZE = max(10, args.page_size)

    url = f"http://{'localhost' if args.host == '0.0.0.0' else args.host}:{args.port}/"
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
