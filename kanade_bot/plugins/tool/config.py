from pydantic import BaseModel


class Config(BaseModel):
    tool_font_path: str = ""
    """字体文件路径，支持OTF和TTF格式。默认为空字符串，表示使用内置字体。"""
