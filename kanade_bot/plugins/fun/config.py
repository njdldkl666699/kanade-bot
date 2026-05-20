from pathlib import Path

from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_file, get_plugin_data_file


class ScopedConfig(BaseModel):
    ciallo_image_file: str = "宵崎奏Ciallo.webp"
    """Ciallo表情图片名"""
    plus_one_threshold: int = 0
    """+1功能的触发阈值，单位为条消息，小于等于0表示禁用"""

    music_list_file: str = "lx_list"
    """音乐列表文件名，LX Music playList_v2格式的JSON文件"""
    music_list_link: str = ""
    """完整音乐列表链接，仅展示用"""

    sing_dir: str = "sing_songs"
    """唱歌功能的歌曲目录名，目录下是每首歌的MP3文件"""
    sing_page_size: int = 10
    """唱歌功能的歌曲列表分页大小，每页显示多少首歌"""

    lyrics_dir: str = "lyrics"
    """歌词列表目录名，目录下是每首歌的.lrc文件"""
    lyric_default_length: int = 4
    """歌词功能默认返回的歌词行数，一行包含原文、翻译和罗马音（如有）"""

    duanzi_list_file: str = "duanzi.json"
    """段子(史)JSON列表文件名"""

    @property
    def ciallo_image_file_path(self) -> Path:
        """Ciallo表情图片文件路径"""
        return get_plugin_config_file(self.ciallo_image_file)

    @property
    def music_list_file_path(self) -> Path:
        """音乐列表文件路径"""
        return get_plugin_data_file(self.music_list_file)

    @property
    def sing_dir_path(self) -> Path:
        """唱歌功能的歌曲目录路径"""
        return get_plugin_data_file(self.sing_dir)

    @property
    def lyrics_dir_path(self) -> Path:
        """歌词列表目录路径"""
        return get_plugin_data_file(self.lyrics_dir)

    @property
    def duanzi_list_file_path(self) -> Path:
        """段子(史)JSON列表文件路径"""
        return get_plugin_data_file(self.duanzi_list_file)


class Config(BaseModel):
    fun: ScopedConfig
