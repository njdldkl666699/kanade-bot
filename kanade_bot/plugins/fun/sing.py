import random
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger
from pydub import AudioSegment

from .config import Config

cfg = get_plugin_config(Config)

sing_songs: list[Path] = []


def get_sing_song_pages():
    """获取歌曲列表的总页数"""
    total_songs = len(sing_songs)
    page_size = cfg.fun_sing_page_size
    total_pages = (total_songs + page_size - 1) // page_size
    return total_pages


def list_sing_songs(query: str | None = None, page: int = 1) -> list[Path]:
    """列出符合query条件的歌曲文件，分页展示，每页10首"""
    if query:
        query = query.lower()
        filtered_songs = [song for song in sing_songs if query in song.stem.lower()]
    else:
        filtered_songs = sing_songs

    # 分页展示
    page_size = cfg.fun_sing_page_size
    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    return filtered_songs[start_index:end_index]


def get_or_random_sing_song(query: str | None = None, number: int | None = None) -> Path | None:
    """列出符合query条件的歌曲文件，并返回随机或指定序号的歌曲文件路径"""
    if query:
        query = query.lower()
        song_files = [song for song in sing_songs if query in song.stem.lower()]
    else:
        song_files = sing_songs

    # 如果指定了序号，返回对应的歌曲
    if number is not None:
        index = number - 1
        if 0 <= index < len(song_files):
            return song_files[index]
        else:
            return None

    # 随机选择一首歌曲
    if not song_files:
        return None
    return random.choice(song_files)


def random_clip_song(song_path: Path) -> AudioSegment:
    """随机裁剪长度为clip_length_ms的片段"""
    audio = AudioSegment.from_file(song_path)
    audio_length = len(audio)
    # 随机裁剪长度
    clip_length_ms = random.randint(5000, 15000)
    if audio_length <= clip_length_ms:
        return audio

    start_ms = random.randint(0, audio_length - clip_length_ms)
    return audio[start_ms : start_ms + clip_length_ms]


driver = get_driver()


@driver.on_startup
def load_sing_songs():
    global sing_songs
    directory = Path(cfg.fun_sing_dir_path)
    if not directory.is_dir():
        logger.warning("唱歌功能的歌曲文件目录不存在，路径: {}", directory.absolute())
        return

    sing_songs = list(directory.glob("*.mp3"))
    logger.info(f"加载唱歌功能的歌曲文件，共 {len(sing_songs)} 首，路径: {directory.absolute()}")
