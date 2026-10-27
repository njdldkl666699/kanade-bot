"""消息二进制内容（图片等）与占位字符串的互转

`SessionStore`持久化时把`BinaryContent`替换为占位并把 bytes 存入 blobs 表，读取边界再还原。
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import BinaryContent, ModelMessage, ModelRequest, UserPromptPart

BLOB_REF_PREFIX = "$kanade-blob:"
"""占位字符串前缀；完整格式 `$kanade-blob:{sha256(media+bytes)}:{mime}`"""

_BLOB_REF_RE = re.compile(rf"^{re.escape(BLOB_REF_PREFIX)}(?P<hash>[0-9a-f]{{64}}):(?P<mime>.+)$")

BlobFetcher = Callable[[str], bytes | None]
"""按 hash 取原始 bytes；未命中返回 None（占位将原样保留）"""


@dataclass(frozen=True, slots=True)
class BlobWrite:
    """一条待写入 blobs 表的二进制内容"""

    hash: str
    mime: str
    data: bytes


def _content_hash(bc: BinaryContent) -> str:
    """mime 参与 hash：同 bytes 不同 mime 的内容视为不同 blob"""
    h = hashlib.sha256()
    h.update(bc.media_type.encode())
    h.update(b"\0")
    h.update(bc.data)
    return h.hexdigest()


def _blob_ref(bc: BinaryContent) -> str:
    return f"{BLOB_REF_PREFIX}{_content_hash(bc)}:{bc.media_type}"


def parse_blob_ref(value: Any) -> re.Match[str] | None:
    """完整匹配占位格式时返回 Match（含 hash / mime 组），否则 None"""
    if not isinstance(value, str):
        return None
    return _BLOB_REF_RE.match(value)


def _map_sequence(items: Sequence[Any], on_binary: Callable[[BinaryContent], Any]):
    """把序列中每个 BinaryContent 映换为 on_binary 的返回值

    :return: (新序列, 是否有改动, 收集到的二进制内容)
    """
    new_items: list[Any] = []
    binaries: list[BinaryContent] = []
    changed = False
    for item in items:
        if isinstance(item, BinaryContent):
            binaries.append(item)
            new_items.append(on_binary(item))
            changed = True
        else:
            new_items.append(item)
    return (new_items if changed else list(items)), changed, binaries


def _map_part_content(
    part: Any, on_binary: Callable[[BinaryContent], Any]
) -> tuple[Any, Any] | None:
    """映射单个 part 中可能出现的 BinaryContent

    覆盖两类位置：
    - `UserPromptPart.content`（str 或多模态序列）
    - 工具返回 part（含 `tool_name` 属性者）的 `content`（可能本身是
      BinaryContent、或含 BinaryContent 的序列）

    :return: (新 part, 收集到的二进制内容)；无需改动时 None
    """
    if isinstance(part, UserPromptPart):
        content = part.content
        if isinstance(content, str):
            return None
        new_items, changed, binaries = _map_sequence(content, on_binary)
        if not changed:
            return None
        return replace(part, content=new_items), binaries

    if hasattr(part, "tool_name"):
        content = part.content
        if isinstance(content, BinaryContent):
            return replace(part, content=on_binary(content)), [content]
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            new_items, changed, binaries = _map_sequence(content, on_binary)
            if not changed:
                return None
            return replace(part, content=new_items), binaries
    return None


def _map_messages(
    messages: Sequence[ModelMessage],
    part_mapper: Callable[[Any], tuple[Any, Any] | None],
) -> tuple[list[ModelMessage], list[BinaryContent]]:
    """遍历消息树应用 part 级映射，不改写传入对象"""
    result: list[ModelMessage] = []
    binaries: list[BinaryContent] = []
    for msg in messages:
        if not isinstance(msg, ModelRequest):
            result.append(msg)
            continue
        new_parts: list[Any] = []
        msg_changed = False
        for part in msg.parts:
            mapped = part_mapper(part)
            if mapped is None:
                new_parts.append(part)
                continue
            new_part, part_binaries = mapped
            new_parts.append(new_part)
            binaries.extend(part_binaries)
            msg_changed = True
        result.append(replace(msg, parts=new_parts) if msg_changed else msg)
    return result, binaries


def externalize_messages(
    messages: Sequence[ModelMessage],
) -> tuple[list[ModelMessage], list[BlobWrite]]:
    """把消息中的 BinaryContent 替换为占位字符串，返回 (新消息列表, 待写 blobs)"""

    externalized, binaries = _map_messages(messages, lambda p: _map_part_content(p, _blob_ref))
    writes = [
        BlobWrite(hash=_content_hash(bc), mime=bc.media_type, data=bc.data) for bc in binaries
    ]
    return externalized, writes


def rehydrate_messages(
    messages: Sequence[ModelMessage],
    fetch: BlobFetcher,
) -> list[ModelMessage]:
    """把占位字符串还原为 BinaryContent；fetch 未命中的占位原样保留"""

    def on_str(value: Any) -> Any:
        m = parse_blob_ref(value)
        if m is None:
            return value
        data = fetch(m.group("hash"))
        if data is None:
            return value
        bc = BinaryContent(data=data, media_type=m.group("mime"))
        # 与反序列化路径保持一致：图片/音频收窄为子类
        return BinaryContent.narrow_type(bc)

    def map_str_sequence(items: Sequence[Any]):
        new_items: list[Any] = []
        changed = False
        for item in items:
            new_item = on_str(item)
            changed = changed or new_item is not item
            new_items.append(new_item)
        return (new_items if changed else None), changed

    def part_mapper(part: Any) -> tuple[Any, Any] | None:
        if isinstance(part, UserPromptPart):
            content = part.content
            if isinstance(content, str):
                return None
            new_items, changed = map_str_sequence(content)
            return (replace(part, content=new_items), []) if changed else None

        if hasattr(part, "tool_name"):
            content = part.content
            if isinstance(content, str) and parse_blob_ref(content):
                return replace(part, content=on_str(content)), []
            if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
                new_items, changed = map_str_sequence(content)
                return (replace(part, content=new_items), []) if changed else None
        return None

    rehydrated, _ = _map_messages(messages, part_mapper)
    return rehydrated


def blob_marker_messages(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """把 BinaryContent 替换为仅含 hash 的标记（不触碰 DB），用于指纹计算

    标记由内容 hash 决定：标记形态相等 ⟺ 还原形态相等，因此指纹语义
    与全量序列化一致，但不再搬运 base64 巨串。
    """

    def marker(bc: BinaryContent) -> str:
        return f"{BLOB_REF_PREFIX}{_content_hash(bc)}"

    marked, _ = _map_messages(messages, lambda p: _map_part_content(p, marker))
    return marked
