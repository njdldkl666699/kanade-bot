from pydantic import BaseModel


class Config(BaseModel):
    fun_ciallo_image_path: str = ""
    """Ciallo表情图片路径"""
    fun_music_list_path: str = ""
    """音乐列表文件路径，LX Music playList_v2格式的JSON文件"""
    fun_music_list_link: str = ""
    """完整音乐列表链接，仅展示用"""
