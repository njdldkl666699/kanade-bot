import argparse
from pathlib import Path
from typing import Any

from pydantic import BaseModel


class CopilotEvent(BaseModel):
    """Copilot会话事件模型"""

    type: str
    data: dict[str, Any]
    id: str
    timestamp: str
    parentId: str | None = None


def replace_model_id(
    new_model_id: str,
    source_path: Path,
    target_path: Path | None = None,
):
    """替换Copilot会话事件模型中的model_id字段"""
    if not source_path.exists():
        raise FileNotFoundError(f"源文件不存在: {source_path}")

    with source_path.open("r", encoding="utf-8") as f:
        source_events = [CopilotEvent.model_validate_json(line) for line in f if line.strip()]

    target_events: list[CopilotEvent] = []
    for event in source_events:
        data = event.data
        if "model" in data:
            data["model"] = new_model_id
        if "selectedModel" in data:
            data["selectedModel"] = new_model_id
        target_events.append(event)

    if not target_path:
        target_path = source_path
    with target_path.open("w", encoding="utf-8") as f:
        for event in target_events:
            f.write(event.model_dump_json() + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Copilot会话事件模型修改工具",
        epilog="""此工具用于修改Copilot Session State `events.jsonl` 中影响对话使用模型的字段。
当你切换模型和提供商后恢复会话报错404时，可以使用此工具将会话事件中的相关替换为新的模型ID，从而允许跨提供商恢复会话。""",
    )
    parser.add_argument(
        "--model-id",
        type=str,
        required=True,
        help="新的模型ID",
    )
    parser.add_argument(
        "source",
        type=Path,
        help="要修改的`events.jsonl`文件路径",
        nargs="?",
    )
    parser.add_argument(
        "target",
        type=Path,
        help="修改后的`events.jsonl`的输出路径，不指定则覆盖源文件",
        nargs="?",
    )
    args = parser.parse_args()
    replace_model_id(
        args.model_id,
        args.source,
        args.target,
    )
