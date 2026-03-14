from pydantic import BaseModel


class Config(BaseModel):
    help_markdown_path: str
    help_image_light_path: str
    help_image_dark_path: str
