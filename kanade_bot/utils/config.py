from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

C = TypeVar("C", bound=BaseModel)
"""JSON配置模型类型变量"""


def ensure_json_config(path: Path, config_cls: type[C]) -> C:
    """确保JSON配置文件存在并且可以被解析为指定的配置模型

    如果配置文件不存在，创建一个默认的配置文件并返回默认配置模型实例。

    :param path: 配置文件路径
    :param config_cls: 配置模型类，必须是BaseModel的子类
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        default_config = config_cls()
        path.write_text(
            default_config.model_dump_json(indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return default_config

    try:
        return config_cls.model_validate_json(path.read_text(encoding="utf-8"))
    except ValidationError as e:
        raise RuntimeError(f"配置文件 {path} 无法解析，请检查文件内容是否正确") from e


def write_json_config(path: Path, config: BaseModel):
    """将配置模型实例写入JSON配置文件

    :param path: 配置文件路径
    :param config: 配置模型实例
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        config.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
