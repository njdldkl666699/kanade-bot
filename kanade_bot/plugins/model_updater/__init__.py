from pathlib import Path
from typing import Any, Generic, TypeVar

import nonebot
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.templating import Jinja2Templates
from nonebot import get_plugin_config, logger, require
from nonebot.plugin import PluginMetadata
from pydantic import BaseModel

from .config import Config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_dir

__plugin_meta__ = PluginMetadata(
    name="model_updater",
    description="提供一个Web界面用于更新模型数据，支持Pydantic模型的自动验证和类型转换",
    usage="",
    config=Config,
)
cfg = get_plugin_config(Config)

T = TypeVar("T", bound=BaseModel, covariant=True)


class ModelRegistryItem(BaseModel, Generic[T]):
    """模型注册表项"""

    cls: type[T]
    """模型类"""
    instance: T
    """实例"""
    path: Path
    """模型对应的 JSON 文件路径"""

    def save_to_file(self):
        """将模型保存到 JSON 文件"""
        logger.debug(f"保存模型 {self.cls.__name__} 到文件 {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            self.instance.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8"
        )


model_registry: list[ModelRegistryItem[BaseModel]] = []
"""模型注册表"""


def load_register_model_from_file(cls: type[T], file_path: Path) -> ModelRegistryItem[T]:
    """从 JSON 文件加载模型并注册，文件不存在时使用默认值并保存

    Returns:
        ModelRegistryItem[T]: 注册表项
    """
    if not file_path.exists():
        logger.warning(f"模型文件 {file_path} 不存在，使用默认值并保存到文件")
        model = cls()
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(model.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
    else:
        model = cls.model_validate_json(file_path.read_text(encoding="utf-8"))

    registered_item = ModelRegistryItem(cls=cls, instance=model, path=file_path)
    model_registry.append(registered_item)
    return registered_item


templates = Jinja2Templates(directory=get_plugin_config_dir())


# 辅助函数：从注册表构建序列化数据（供模板和 API 复用）
def get_registry_data():
    data = []
    for item in model_registry:
        data.append(
            {
                "name": item.cls.__name__,
                "fields": list(item.cls.model_fields.keys()),
                "data": item.instance.model_dump(mode="json"),  # 转为 dict
            }
        )
    return data


model_updater = APIRouter(prefix="/model_updater", tags=["model_updater"])


# 首页：渲染模板，传入注册表数据
@model_updater.get("/")
async def home(request: Request):
    registry_data = get_registry_data()
    return templates.TemplateResponse(
        request,
        name=cfg.model_updater_template_file,
        context={"registry": registry_data},
    )


@model_updater.get("/registry")
async def get_registry():
    """返回当前注册表中所有模型的序列化数据"""
    return get_registry_data()  # 复用之前定义的辅助函数


# 更新接口
@model_updater.post("/")
async def update_model(payload: dict[str, Any]):
    model_name = payload.get("model_name")
    new_data = payload.get("data")
    if not model_name or not new_data:
        raise HTTPException(400, "缺少 model_name 或 data")

    # 根据类名查找对应的注册表项
    target_class = None
    target_item = None
    for item in model_registry:
        if item.cls.__name__ == model_name:
            target_class = item.cls
            target_item = item
            break

    if not target_class or not target_item:
        raise HTTPException(404, f"未找到模型 {model_name}")

    try:
        # 使用 Pydantic 自动验证和转换类型
        new_instance = target_class.model_validate(new_data)
        target_item.instance = new_instance  # 更新指针指向的值
        target_item.save_to_file()  # 保存到文件
    except Exception as e:
        raise HTTPException(400, f"数据验证失败: {str(e)}")

    return {"status": "success", "message": f"{model_name} 已更新"}


app: FastAPI = nonebot.get_app()
app.include_router(model_updater)

__all__ = [
    "ModelRegistryItem",
    "load_register_model_from_file",
]
