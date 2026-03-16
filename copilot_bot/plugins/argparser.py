from typing import Any

from nonebot.adapters import Message
from nonebot.params import CommandArg


def parse_arg_message(
    arg_message: Message = CommandArg(),
    mappings: dict[str, type] | None = None,
) -> dict[str, Any]:
    """解析命令参数消息

    参数:
        arg_message: 命令参数消息
        mappings: 可选的参数名称映射字典，键为参数名称，值为参数类型

    返回:
        dict: 解析后的参数字典，键为参数名称，值为参数值；\
            如果参数值无法转换为指定类型或缺失，则值为 None

    示例:

        >>> parse_arg_message(Message("北京 3"), {"query": str, "days": int})
        {"query": "北京", "days": 3}
        >>> parse_arg_message(Message("上海"), {"query": str, "days": int})
        {"query": "上海", "days": None}

    """
    if not mappings:
        return {}

    args = arg_message.extract_plain_text().strip().split()
    arg_dict: dict[str, Any] = {}

    for index, (name, value_type) in enumerate(mappings.items()):
        if index >= len(args):
            arg_dict[name] = None
            continue

        raw_value = args[index]
        try:
            arg_dict[name] = value_type(raw_value)
        except (TypeError, ValueError):
            arg_dict[name] = None

    return arg_dict
