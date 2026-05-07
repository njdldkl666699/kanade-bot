from pydantic import BaseModel


class Config(BaseModel):
    fun_ciallo_image_file_path: str = "assets/images/宵崎奏Ciallo.webp"
    """Ciallo表情图片路径"""
    fun_plus_one_threshold: int = 0
    """+1功能的触发阈值，单位为条消息，小于等于0表示禁用"""

    fun_music_list_file_path: str = "assets/lx_list"
    """音乐列表文件路径，LX Music playList_v2格式的JSON文件"""
    fun_music_list_link: str = ""
    """完整音乐列表链接，仅展示用"""

    fun_sing_dir_path: str = "assets/sing_songs"
    """唱歌功能的歌曲文件目录，目录下是每首歌的MP3文件"""
    fun_sing_page_size: int = 10
    """唱歌功能的歌曲列表分页大小，每页显示多少首歌"""

    fun_lyrics_dir_path: str = "assets/lyrics"
    """歌词列表目录，目录下是每首歌的.lrc文件"""
    fun_lyric_default_length: int = 4
    """歌词功能默认返回的歌词行数，一行包含原文、翻译和罗马音（如有）"""

    fun_duanzi_list_file_path: str = "assets/duanzi.json"
    """段子(史)列表文件路径，JSON列表文件"""
