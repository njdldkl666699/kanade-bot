from pydantic import BaseModel


class Config(BaseModel):
    model_updater_template_file: str = "template.html"
    """模型更新器使用的HTML模板文件名，支持HTML格式。"""
