import ast
import inspect
import json
from pathlib import Path
from typing import ClassVar

from nonebot import get_driver, get_plugin_config, logger
from nonebot.config import Config as NoneBotConfig
from pydantic import BaseModel, create_model

from scripts.github_watchdog import Config as WatchdogConfig


class AttrDocModel(BaseModel):
    """带有属性docstring的Pydantic模型基类"""

    model_config = {"use_attribute_docstrings": True}


class SchemaConfig(AttrDocModel):
    """JSON Schema生成配置"""

    generate_schemas: bool = False
    """是否生成JSON Schema文件"""
    schema_output_dir: str = "schemas/"
    """JSON Schema输出目录，默认为`schemas/`"""

    @property
    def schema_output_dir_path(self) -> Path:
        """JSON Schema输出目录路径"""
        return Path(self.schema_output_dir)


def generate_schema[T: BaseModel](cls: type[T]):
    """生成JSON Schema文件"""
    cfg = get_plugin_config(SchemaConfig)
    if not cfg.generate_schemas:
        return

    schema_filename = f"{cls.__name__}.json"
    logger.info(f"正在生成JSON Schema文件: {schema_filename}")
    output_dir_path = cfg.schema_output_dir_path
    schema_file = output_dir_path / schema_filename
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    json_schema = json.dumps(cls.model_json_schema(), indent=2, ensure_ascii=False)
    schema_file.write_text(json_schema, encoding="utf-8")


def _extract_docstrings(cls: type[BaseModel]) -> dict[str, str]:
    """从源码提取字段的文档字符串（支持 AnnAssign 和 Assign）"""
    try:
        tree = ast.parse(inspect.getsource(cls))
    except OSError:
        return {}

    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls.__name__:
            docs: dict[str, str] = {}
            # 遍历类体，查找字段定义后紧跟的字符串常量
            for i, item in enumerate(node.body[:-1]):  # 避免越界
                # 判断是否为字段定义（带/不带类型注解）
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    field_name = item.target.id
                elif (
                    isinstance(item, ast.Assign)
                    and len(item.targets) == 1
                    and isinstance(item.targets[0], ast.Name)
                ):
                    field_name = item.targets[0].id
                else:
                    continue

                # 检查下一个节点是否为字符串字面量
                next_node = node.body[i + 1]
                if isinstance(next_node, ast.Expr) and isinstance(next_node.value, ast.Constant):
                    docs[field_name] = str(next_node.value.value).strip()
            return docs
    return {}


class ConfigRegistry:
    config_types: ClassVar[list[type[BaseModel]]] = [NoneBotConfig, WatchdogConfig, SchemaConfig]
    """插件配置类型注册表"""

    @classmethod
    def register_config_types(cls, *config_type: type[BaseModel]):
        """注册插件配置类型"""
        cls.config_types.extend(config_type)

    @classmethod
    def generate_merged_config_schema(cls, name: str = "MergedConfig"):
        """生成合并后的NoneBot Config和插件配置JSON Schema文件"""
        cfg = get_driver().config
        if not cfg.generate_schemas:
            return

        fields = {}
        for config_type in cls.config_types:
            # 检查是否设置了use_attribute_docstrings，未设置则从源码提取字段的文档字符串
            use_doc = config_type.model_config.get("use_attribute_docstrings", False)
            doc_map = _extract_docstrings(config_type) if not use_doc else {}

            for field_name, field_info in config_type.model_fields.items():
                if not use_doc and field_info.description is None and field_name in doc_map:
                    # 复制一份，避免修改原模型
                    new_field_info = field_info._copy()
                    new_field_info.description = doc_map[field_name]
                else:
                    new_field_info = field_info
                fields[field_name] = (field_info.annotation, new_field_info)

        MergedConfig = create_model(name, __config__=AttrDocModel.model_config, **fields)
        generate_schema(MergedConfig)
