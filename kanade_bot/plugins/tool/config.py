from pydantic import BaseModel


class Config(BaseModel):
    tool_fallback_icon_file_path: str = "assets/images/grass_block.png"
    """服务器图标加载失败时的替代图标路径，支持PNG格式。"""
    tool_template_file_path: str = "assets/mcstatus_template.html"
    """服务器状态渲染使用的HTML模板路径，支持HTML格式。"""
