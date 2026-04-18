from pydantic import BaseModel


class Config(BaseModel):
    help_docs_dir_path: str = "assets/helps/"
    """帮助文档所在目录"""
    help_images_cache_dir_path: str = "assets/help_images"
    """帮助文档图片的缓存目录，过期会自动更新"""
    help_sakura_bot_link: str = ""
    """Sakura bot的帮助文档链接"""
