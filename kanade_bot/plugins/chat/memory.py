import re
from pathlib import Path
from threading import Lock
from typing import Literal

from nonebot import logger

from ..util import SessionInfo
from .config import cfg

MemoryType = Literal["user", "group", "session"]
WriteMode = Literal["replace", "append"]

MEMORIES_DIR = Path(cfg.chat_memories_dir_path)
_memory_contexts: dict[str, SessionInfo] = {}
_memory_lock = Lock()


def set_memory_context(session_info: SessionInfo):
    """记录当前 Copilot 会话对应的聊天上下文，供记忆工具解析用户/群聊。"""
    with _memory_lock:
        _memory_contexts[session_info.session_id] = session_info


def delete_session_memory(session_id: str):
    """删除随 Copilot 会话生命周期存在的会话记忆。"""
    path = _memory_path("session", session_id=session_id)
    with _memory_lock:
        _memory_contexts.pop(session_id, None)
        if path.is_file():
            path.unlink()


def read_memory_content(memory_type: MemoryType, session_id: str) -> str:
    path = _get_memory_path(memory_type, session_id)
    with _memory_lock:
        if not path.is_file():
            logger.info("读取{}记忆，文件不存在：{}", memory_type, path)
            return ""

        content = path.read_text(encoding="utf-8")

    logger.info(
        "读取{}记忆，路径：{}，字符数：{}",
        memory_type,
        path,
        len(content),
    )
    return content


def write_memory_content(
    memory_type: MemoryType,
    session_id: str,
    content: str,
    mode: WriteMode,
):
    path = _get_memory_path(memory_type, session_id)
    with _memory_lock:
        path.parent.mkdir(parents=True, exist_ok=True)

        memory_content = content.strip()
        if mode == "append" and path.is_file():
            existing = path.read_text(encoding="utf-8").rstrip()
            memory_content = (
                f"{existing}\n\n{memory_content}"
                if existing and memory_content
                else existing or memory_content
            )

        path.write_text(f"{memory_content}\n" if memory_content else "", encoding="utf-8")

    logger.info(
        "写入{}记忆，模式：{}，路径：{}，字符数：{}",
        memory_type,
        mode,
        path,
        len(memory_content),
    )


def _get_memory_path(memory_type: MemoryType, session_id: str) -> Path:
    with _memory_lock:
        session_info = _memory_contexts.get(session_id)
    return _memory_path(
        memory_type,
        session_id=session_id,
        session_info=session_info,
    )


def _safe_name(value: str) -> str:
    value = value.strip()
    if not value:
        return "unknown"
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", value)


def _memory_path(
    memory_type: MemoryType,
    *,
    session_id: str,
    session_info: SessionInfo | None = None,
) -> Path:
    if memory_type == "session":
        return MEMORIES_DIR / "sessions" / f"{_safe_name(session_id)}.md"

    if session_info is None:
        raise ValueError("当前工具调用没有可用的聊天上下文")
    if not session_info.platform:
        raise ValueError("当前上下文没有平台信息，无法访问长期记忆")

    if memory_type == "user":
        if not session_info.user_id:
            raise ValueError("当前上下文没有用户 id，无法访问用户记忆")
        filename = f"{_safe_name(session_info.platform)}-{_safe_name(session_info.user_id)}.md"
        return MEMORIES_DIR / "users" / filename

    if memory_type == "group":
        if not session_info.group_id:
            raise ValueError("当前上下文没有群聊 id，无法访问群聊记忆")
        filename = f"{_safe_name(session_info.platform)}-{_safe_name(session_info.group_id)}.md"
        return MEMORIES_DIR / "groups" / filename

    raise ValueError(f"未知的记忆类型：{memory_type}")
