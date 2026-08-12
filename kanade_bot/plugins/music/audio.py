import asyncio
import random
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config).music

sing_songs: list[Path] = []


def get_audio_pages():
    """获取歌曲列表的总页数"""
    total_songs = len(sing_songs)
    page_size = cfg.audio_page_size
    total_pages = (total_songs + page_size - 1) // page_size
    return total_pages


def query_audios(query: str | None = None, page: int = 1) -> list[Path]:
    """列出符合query条件的歌曲文件，分页展示，每页10首"""
    if query:
        query = query.lower()
        filtered_songs = [song for song in sing_songs if query in song.stem.lower()]
    else:
        filtered_songs = sing_songs

    # 分页展示
    page_size = cfg.audio_page_size
    start_index = (page - 1) * page_size
    end_index = start_index + page_size
    return filtered_songs[start_index:end_index]


def get_or_random_audio(query: str | None = None, number: int | None = None) -> Path | None:
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


async def random_clip_audio(song_path: Path) -> bytes:
    """使用 FFmpeg 只解码并编码随机片段，避免加载整首歌曲。"""
    probe = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(song_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await probe.communicate()
    if probe.returncode != 0:
        raise RuntimeError(f"无法读取音频时长：{stderr.decode(errors='replace').strip()}")

    try:
        audio_length = max(0.0, float(stdout.strip()))
    except ValueError as exc:
        raise RuntimeError("无法解析音频时长") from exc

    clip_length = random.randint(5000, 15000) / 1000
    clip_length = min(clip_length, audio_length)
    start = 0.0
    if audio_length > clip_length:
        start = random.uniform(0, audio_length - clip_length)

    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{clip_length:.3f}",
        "-i",
        str(song_path),
        "-vn",
        "-c:a",
        "libmp3lame",
        "-f",
        "mp3",
        "pipe:1",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    encoded, stderr = await process.communicate()
    if process.returncode != 0 or not encoded:
        raise RuntimeError(f"音频片段转码失败：{stderr.decode(errors='replace').strip()}")
    return encoded


driver = get_driver()


@driver.on_startup
def load_sing_songs():
    global sing_songs
    path = cfg.audios_dir_path
    if not path.is_dir():
        logger.warning("唱歌功能的歌曲文件目录不存在，路径: {}", path.absolute())
        return

    sing_songs = list(path.glob("*.mp3"))
    logger.info(f"加载唱歌功能的歌曲文件，共 {len(sing_songs)} 首，路径: {path.absolute()}")
