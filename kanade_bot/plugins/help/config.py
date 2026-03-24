from pydantic import BaseModel


class Config(BaseModel):
    help_link: str = ""
    """帮助文档链接"""
