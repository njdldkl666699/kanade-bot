import ast
import inspect
import json
from pathlib import Path
from typing import Any, ClassVar

from nonebot import get_driver, get_plugin_config, logger
from nonebot.config import Config as NoneBotConfig
from nonebot.config import Env
from openai.types import ReasoningEffort
from pydantic import BaseModel, create_model
from pydantic.fields import FieldInfo

from scripts.github_watchdog import Config as WatchdogConfig


class AttrDocModel(BaseModel):
    """带有属性docstring的Pydantic模型基类"""

    model_config = {"use_attribute_docstrings": True}


class ProviderConfig(AttrDocModel):
    """OpenAI兼容API提供商配置"""

    base_url: str | None = None
    """API Base URL，为None时使用openai官方端点（需设置OPENAI_API_KEY环境变量）"""

    api_key: str | None = None
    """API密钥，为None时回退到OPENAI_API_KEY环境变量"""

    headers: dict[str, str] | None = None
    """额外请求头"""

    supports_max_completion_tokens: bool = False
    """Chat Completions端点是否接受`max_completion_tokens`字段"""


class MCPServerConfig(AttrDocModel):
    """MCP服务器配置（Streamable HTTP传输）"""

    url: str
    """MCP服务器URL"""

    headers: dict[str, str] | None = None
    """请求头，如Authorization"""

    tools: list[str] | None = None
    """工具白名单，None或["*"]表示全部工具"""


class BaseAgentConfig(AttrDocModel):
    """基础Agent配置"""

    model: str = ""
    """模型ID"""

    provider: ProviderConfig | None = None
    """模型提供商配置，如果为None则使用openai官方端点"""

    reasoning_effort: ReasoningEffort = None
    """推理努力程度，仅对支持的模型生效"""

    max_output_tokens: int | None = None
    """模型单次响应的最大输出token数"""

    context_window: int | None = None
    """上下文窗口"""

    vision: bool = False
    """模型是否支持图片（视觉）输入"""

    mcp_servers: dict[str, MCPServerConfig] | None = None
    """MCP服务器配置，键为服务器名称"""


class KanadeConfig(AttrDocModel):
    """宵崎奏Bot额外全局配置"""

    generate_schemas: bool = False
    """是否生成JSON Schema文件"""
    schema_output_dir: str = "schemas/"
    """JSON Schema输出目录"""
    autoclear_cache_dir: str = "auto_clear/"
    """自动清理的缓存目录"""
    print_kanade_banner: bool = True
    """是否打印宵崎奏Bot的启动横幅"""
    print_pydantic_ai_banner: bool = True
    """是否打印Pydantic AI首次运行Agent时的横幅"""

    @property
    def schema_output_dir_path(self) -> Path:
        """JSON Schema输出目录路径"""
        return Path(self.schema_output_dir)

    @property
    def autoclear_cache_dir_path(self) -> Path:
        """自动清理的缓存目录路径"""
        from nonebot_plugin_localstore import BASE_CACHE_DIR

        p = BASE_CACHE_DIR / self.autoclear_cache_dir
        p.mkdir(parents=True, exist_ok=True)
        return p


def generate_schema[T: BaseModel](cls: type[T]):
    """生成JSON Schema文件"""
    cfg = get_plugin_config(KanadeConfig)
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
    config_types: ClassVar[list[type[BaseModel]]] = [
        Env,
        NoneBotConfig,
        KanadeConfig,
        WatchdogConfig,
    ]
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

        fields: dict[str, tuple[type[Any] | None, FieldInfo]] = {}
        for config_type in cls.config_types:
            use_doc = config_type.model_config.get("use_attribute_docstrings", False)
            doc_map = _extract_docstrings(config_type) if not use_doc else {}

            for field_name, field_info in config_type.model_fields.items():
                # 准备 field_info（可能补充 docstring）
                if not use_doc and field_info.description is None and field_name in doc_map:
                    new_field_info = field_info._copy()
                    new_field_info.description = doc_map[field_name]
                else:
                    new_field_info = field_info

                # 若字段已存在，合并描述（保留非空）
                if field_name in fields:
                    existing_anno, existing_info = fields[field_name]
                    # 只有当现有描述为空，且新描述非空时，才更新描述
                    if existing_info.description is None and new_field_info.description is not None:
                        updated_info = existing_info._copy()
                        updated_info.description = new_field_info.description
                        fields[field_name] = (existing_anno, updated_info)
                    # 否则保留原字段（已有描述或新描述为空）
                else:
                    fields[field_name] = (field_info.annotation, new_field_info)

        sorted_fields = dict(sorted(fields.items()))
        MergedConfig = create_model(name, __config__=AttrDocModel.model_config, **sorted_fields)  # type: ignore
        generate_schema(MergedConfig)
