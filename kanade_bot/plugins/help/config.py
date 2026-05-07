from pydantic import BaseModel


class Config(BaseModel):
    help_docs_dir_path: str = "assets/helps/"
    """帮助文档所在目录"""
    help_images_cache_dir_path: str = "assets/help_images/"
    """帮助文档图片的缓存目录，过期会自动更新"""
    help_haruki_image_file_path: str = "assets/images/Haruki_help.png"
    """Haruki的帮助图片路径，不存在则不发送"""
