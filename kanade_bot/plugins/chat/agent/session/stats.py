"""会话统计辅助：工作区树形列表与压缩空壳消息统计"""

from pathlib import Path

from pydantic_ai import ModelMessage, ToolReturnPart

MAX_WORKSPACE_ENTRIES = 10
"""会话统计最多展示的工作区条目数"""

WORKSPACE_WALK_DEPTH = 3
"""会话统计遍历工作区的最大目录深度"""

EXCLUDED_WORKSPACE_DIRS = frozenset({"images", "rendered"})
"""会话统计整目录排除的顶层目录：工具暂存/生成内容（用户图片暂存、HTML渲染输出）"""

CLEARED_TOOL_RESULT_PLACEHOLDER = "[tool result cleared]"
"""零成本档压缩后的工具结果占位文本（`pydantic_ai_harness` 默认值）"""


def count_emptied_messages(messages: list[ModelMessage]) -> int:
    """统计被零成本档清空的「空壳」消息条数（全部 part 均为清除占位）"""
    count = 0
    for m in messages:
        parts = getattr(m, "parts", ())
        if parts and all(
            isinstance(part, ToolReturnPart) and part.content == CLEARED_TOOL_RESULT_PLACEHOLDER
            for part in parts
        ):
            count += 1
    return count


def _format_size(size: int) -> str:
    """字节数转人类可读文本"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def list_workspace_files(root: Path) -> list[str]:
    """以 tree 风格列出工作区条目（限深限量），返回带树形连接符的文本行"""
    entries: list[str] = []
    truncated = False

    def walk(directory: Path, prefix: str, depth: int) -> None:
        nonlocal truncated
        if truncated or depth >= WORKSPACE_WALK_DEPTH:
            return
        try:
            children = sorted(directory.iterdir(), key=lambda p: p.name)
        except OSError:
            return
        # `.` 开头的隐藏条目默认不展示（同 tree 无 -a，如沙箱的 .venv/.home）
        children = [p for p in children if not p.name.startswith(".")]
        if depth == 0:
            children = [p for p in children if p.name not in EXCLUDED_WORKSPACE_DIRS]
        for i, path in enumerate(children):
            last = i == len(children) - 1
            connector = "└ " if last else "├ "
            if path.is_dir():
                entries.append(f"{prefix}{connector}{path.name}/")
            else:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                entries.append(f"{prefix}{connector}{path.name} ({_format_size(size)})")
            if len(entries) >= MAX_WORKSPACE_ENTRIES:
                truncated = True
                entries.append("…")
                return
            if path.is_dir():
                walk(path, prefix + ("  " if last else "│ "), depth + 1)

    walk(root, "", 0)
    return entries
