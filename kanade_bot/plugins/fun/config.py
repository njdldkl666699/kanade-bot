from pydantic import BaseModel


class Config(BaseModel):
    fun_ciallo_image_path: str = ""
    """Ciallo表情图片路径"""
    fun_music_list_path: str = ""
    """音乐列表文件路径，LX Music playList_v2格式的JSON文件"""
    fun_music_list_link: str = ""
    """完整音乐列表链接，仅展示用"""
    fun_plus_one_threshold: int = 0
    """+1功能的触发阈值，单位为条消息，小于等于0表示禁用"""
    fun_sing_directory: str = ""
    """唱歌功能的歌曲文件目录，目录下是每首歌的MP3文件"""
    fun_sing_clip_length_ms: int = 5000
    """唱歌功能的歌曲裁剪长度，单位为毫秒"""
    fun_sing_page_size: int = 10
    """唱歌功能的歌曲列表分页大小，每页显示多少首歌"""
