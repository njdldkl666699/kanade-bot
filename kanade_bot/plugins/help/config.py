from pydantic import BaseModel


class Config(BaseModel):
    help_doc_path: str = "assets/Kanade-Bot-帮助文档.md"
    """帮助文档路径"""
    help_pic_cache_dir: str = "assets/"
    """帮助文档图片缓存目录，生成的图片会保存在这里，过期会自动更新"""
