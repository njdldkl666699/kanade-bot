from pydantic import BaseModel


class Config(BaseModel):
    tool_fallback_icon_path: str
    """服务器图标加载失败时的替代图标路径，支持PNG格式。"""
    tool_template_path: str
    """服务器状态渲染使用的HTML模板路径，支持HTML格式。"""
