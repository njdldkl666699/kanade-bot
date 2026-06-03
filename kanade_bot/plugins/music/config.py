from pathlib import Path

from nonebot import require
from pydantic import BaseModel

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_data_file


class ScopedConfig(BaseModel):
    playlist_file: str = "lx_list"
    """音乐列表文件名，LX Music playList_v2格式的JSON文件"""
    playlist_link: str = ""
    """完整音乐列表链接，仅展示用"""

    audios_dir: str = "audios/"
    """唱歌功能的歌曲目录名，目录下是每首歌的MP3文件"""
    audio_page_size: int = 10
    """唱歌功能的歌曲列表分页大小，每页显示多少首歌"""

    lyrics_dir: str = "lyrics/"
    """歌词列表目录名，目录下是每首歌的.lrc文件"""
    lyric_default_length: int = 4
    """歌词功能默认返回的歌词行数，一行包含原文、翻译和罗马音（如有）"""

    @property
    def playlist_file_path(self) -> Path:
        """音乐列表文件路径"""
        return get_plugin_data_file(self.playlist_file)

    @property
    def audios_dir_path(self) -> Path:
        """唱歌功能的歌曲目录路径"""
        return get_plugin_data_file(self.audios_dir)

    @property
    def lyrics_dir_path(self) -> Path:
        """歌词列表目录路径"""
        return get_plugin_data_file(self.lyrics_dir)


class Config(BaseModel):
    music: ScopedConfig = ScopedConfig()
