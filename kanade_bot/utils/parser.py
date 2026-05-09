from typing import Any, SupportsIndex


def parse_arg_message(
    arg_str: str,
    mappings: dict[str, type] | None = None,
    maxsplit: SupportsIndex = -1,
) -> dict[str, Any]:
    """解析命令参数消息

    参数:
        arg_str: 命令参数消息
        mappings: 可选的参数名称映射字典，键为参数名称，值为参数类型
        maxsplit: 分割参数消息时的最大分割次数，默认为 -1（不限制）

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

    args = arg_str.strip().split(maxsplit=maxsplit)
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


def bool_from_str(s: str | None) -> bool:
    """将字符串转换为布尔值，支持常见的真值和假值表示"""
    if s is None:
        return False
    s = s.strip().lower()
    if s in {"true", "1", "yes", "y", "on"}:
        return True
    if s in {"false", "0", "no", "n", "off"}:
        return False
    raise ValueError(f"无法将字符串 '{s}' 转换为布尔值")


def build_sender_info(name: str | None, id: str | None) -> str:
    """构建发送者信息字符串"""
    parts: list[str] = []
    if name:
        parts.append(name)
    if id:
        parts.append(f"[id={id}]")
    return "".join(parts)
