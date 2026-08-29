import ast
import inspect
import json
from pathlib import Path
from typing import Any, ClassVar, Literal

from copilot import MCPServerConfig, ModelCapabilitiesOverride, PermissionHandler
from copilot.session import AzureProviderOptions, ReasoningEffort
from nonebot import get_driver, get_plugin_config, logger
from nonebot.config import Config as NoneBotConfig
from nonebot.config import Env
from pydantic import BaseModel, create_model
from pydantic.fields import FieldInfo

from scripts.github_watchdog import Config as WatchdogConfig


class AttrDocModel(BaseModel):
    """带有属性docstring的Pydantic模型基类"""

    model_config = {"use_attribute_docstrings": True}


class ProviderConfig(AttrDocModel):
    """自定义API提供商配置

    此模型用于Pydantic校验，运行时通过`.model_dump()`方法获取字典形式的配置。

    修改自`copilot.session.ProviderConfig`，仅保留了可生成schema的字段。
    """

    type: Literal["openai", "azure", "anthropic"] | None = None
    wire_api: Literal["completions", "responses"] | None = None

    transport: Literal["http", "websockets"] | None = None
    """Transport for OpenAI Responses requests. Defaults to "http". Set 
    "websockets" to deliver Responses API requests over a persistent WebSocket
    connection instead of HTTP. Applies to OpenAI-compatible providers using
    wire_api "responses"."""
    base_url: str | None = None
    api_key: str | None = None

    bearer_token: str | None = None
    """Bearer token for authentication. Sets the Authorization header directly.
    Use this for services requiring bearer token auth instead of API key.
    Takes precedence over api_key when both are set."""
    azure: AzureProviderOptions | None = None
    """Azure-specific options"""
    headers: dict[str, str] | None = None

    model_id: str | None = None
    """Well-known model name used by the runtime to look up agent configuration
    (tools, prompts, reasoning behavior) and default token limits. Also used
    as the wire model when wire_model is not set.
    Falls back to SessionConfig.model."""

    wire_model: str | None = None
    """Model name sent to the provider API for inference. Use this when the
    provider's model name (e.g. an Azure deployment name or a custom
    fine-tune name) differs from model_id.
    Falls back to model_id, then SessionConfig.model."""

    max_prompt_tokens: int | None = None
    """Overrides the resolved model's default max prompt tokens. The runtime
    triggers conversation compaction before sending a request when the prompt
    (system message, history, tool definitions, user message) would exceed
    this limit."""

    max_output_tokens: int | None = None
    """Overrides the resolved model's default max output tokens. When hit, the
    model stops generating and returns a truncated response."""


class BaseAgentConfig(AttrDocModel):
    """基础Agent配置"""

    model: str | None = None
    """模型ID"""
    provider: ProviderConfig | None = None
    """模型提供商配置，如果为None则使用Copilot内置模型的默认值"""
    reasoning_effort: ReasoningEffort | None = None
    """推理努力程度"""
    model_capabilities: ModelCapabilitiesOverride | None = None
    """模型能力覆盖配置"""
    system_prompt_file: str
    """系统提示词文件名"""

    available_tools: list[str] | None = None
    """启用的工具白名单。若指定此列表，则仅指定的工具和chat插件内置工具可用，
    未指定的Copilot CLI内置工具和MCP工具将被排除。
    此选项优先级高于 `excluded_tools`（排除工具列表）。"""
    excluded_tools: list[str] | None = None
    """要禁用的工具列表。适用于所有工具。如果设置了`available_tools`，则忽略此列表。"""
    mcp_servers: dict[str, MCPServerConfig] | None = None
    """MCP服务器配置"""

    def model_dump_session_config(self) -> dict[str, Any]:
        """将配置转换为Copilot SessionConfig字典

        作用为移除`system_prompt_file`字段，并设置额外的默认值
        """
        data = self.model_dump(exclude_unset=True)
        data.pop("system_prompt_file", None)
        data.update(
            {
                "on_permission_request": PermissionHandler.approve_all,
                "large_output": {"enabled": False},
            }
        )
        return data


class KanadeConfig(AttrDocModel):
    """宵崎奏Bot额外全局配置"""

    generate_schemas: bool = False
    """是否生成JSON Schema文件"""
    schema_output_dir: str = "schemas/"
    """JSON Schema输出目录"""
    image_cache_dir: str = "kanade_images/"
    """图片缓存目录"""

    @property
    def schema_output_dir_path(self) -> Path:
        """JSON Schema输出目录路径"""
        return Path(self.schema_output_dir)

    @property
    def image_cache_dir_path(self) -> Path:
        """图片缓存目录路径"""
        from nonebot_plugin_localstore import BASE_CACHE_DIR

        p = BASE_CACHE_DIR / self.image_cache_dir
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
