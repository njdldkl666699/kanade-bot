import random
import re
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger
from pydantic import BaseModel

from .config import Config

cfg = get_plugin_config(Config)


class LyricLine(BaseModel):
    line: str
    """歌词文本"""
    translation: str | None = None
    """歌词翻译文本"""
    romaji: str | None = None
    """歌词罗马音文本"""

    @property
    def pretty_string(self) -> str:
        return (f"{self.line}\n" + (f"{self.translation}\n" if self.translation else "")).strip()


type Lyric = list[LyricLine] | str

# 歌词缓存，键为歌曲文件名（不带扩展名），值为LyricLine对象列表或字符串歌词列表
lyrics: dict[str, Lyric] = {}


def query_lyric_songs(query: str | None = None) -> list[str] | None:
    """查询歌词列表中包含query的歌曲，返回匹配的歌曲名列表"""
    if not lyrics:
        raise ValueError("歌词列表未加载")

    if not query:
        return list(lyrics.keys())

    query = query.lower()
    matched_songs = [song for song in lyrics if query in song.lower()]
    return matched_songs if matched_songs else None


def get_random_lyric(
    query: str | None = None, length: int | None = None
) -> tuple[str, Lyric] | None:
    """查询歌词列表中包含query的歌曲，随机选择一首，并返回歌名和随机裁剪的歌词片段"""
    if length is None:
        length = cfg.fun_lyric_default_length

    song_list = query_lyric_songs(query)
    if not song_list:
        return None

    # 随机选择一首歌曲
    selected_song = random.choice(song_list)
    song_lyrics = lyrics[selected_song]

    # 如果歌词是字符串，直接返回
    if isinstance(song_lyrics, str):
        return selected_song, song_lyrics

    # 随机裁剪歌词片段
    if length >= len(song_lyrics):
        return selected_song, song_lyrics
    start_index = random.randint(0, len(song_lyrics) - length)
    return selected_song, song_lyrics[start_index : start_index + length]


def add_song_lyric_txt(song_name: str, lyric: str):
    """添加歌曲歌词到歌词列表，song_name为歌曲名，lyric为txt歌词文本"""
    song_name = song_name.strip()
    if not song_name:
        raise ValueError("歌曲名不能为空")

    normalized_song_name = _normalize_song_name(song_name)

    lyric = lyric.strip()
    if not lyric:
        raise ValueError("歌词内容不能为空")
    lyrics[normalized_song_name] = lyric

    lyric_dir = Path(cfg.fun_lyrics_directory)
    lyric_dir.mkdir(parents=True, exist_ok=True)
    lyric_file = lyric_dir / f"{normalized_song_name}.txt"
    lyric_file.write_text(lyric, encoding="utf-8")

    logger.info(f"已添加歌词: {song_name} -> {normalized_song_name} ({lyric_file})")


def remove_song_lyric(song_name: str):
    """从歌词列表中移除歌曲"""
    song_name = song_name.strip()
    if not song_name:
        raise ValueError("歌曲名不能为空")

    normalized_song_name = _normalize_song_name(song_name)

    lyrics.pop(song_name, None)
    lyrics.pop(normalized_song_name, None)

    lyric_dir = Path(cfg.fun_lyrics_directory)
    txt_file = lyric_dir / f"{normalized_song_name}.txt"
    lrc_file = lyric_dir / f"{normalized_song_name}.lrc"

    for lyric_file in (txt_file, lrc_file):
        if lyric_file.exists():
            lyric_file.unlink()

    logger.info(f"已删除歌词: {song_name} -> {normalized_song_name}")


TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}:\d{2}(?:\.\d{1,3})?)\]")
METADATA_PATTERN = re.compile(r"^\[[a-zA-Z]+:.*\]$")
INVALID_FILENAME_CHARS_PATTERN = re.compile(r"[\\/*?\"<>|\n\r\t]")


def _normalize_song_name(song_name: str) -> str:
    """将歌曲名标准化为安全文件名，避免路径分隔符导致写入失败。"""
    normalized = INVALID_FILENAME_CHARS_PATTERN.sub("_", song_name).strip().strip(".")
    if not normalized:
        raise ValueError("歌曲名仅包含非法字符，无法保存")
    return normalized


def _timestamp_to_seconds(timestamp: str) -> float:
    minutes_str, seconds_str = timestamp.split(":", maxsplit=1)
    return int(minutes_str) * 60 + float(seconds_str)


def _is_romanized_line(text: str) -> bool:
    # 罗马音通常由ASCII字母和空格组成
    return bool(text) and all(ch.isascii() for ch in text)


def _parse_lrc_sections(content: str) -> list[dict[float, str]]:
    sections: list[dict[float, str]] = []
    current: dict[float, str] = {}

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            if current:
                sections.append(current)
                current = {}
            continue

        if METADATA_PATTERN.match(line):
            continue

        timestamp_matches = list(TIMESTAMP_PATTERN.finditer(line))
        if not timestamp_matches:
            continue

        text = TIMESTAMP_PATTERN.sub("", line).strip()
        if not text:
            continue

        for match in timestamp_matches:
            key = _timestamp_to_seconds(match.group(1))
            current[key] = text

    if current:
        sections.append(current)

    return sections


def _parse_lrc_file(file_path: Path) -> list[LyricLine]:
    content = file_path.read_text(encoding="utf-8")
    sections = _parse_lrc_sections(content)
    if not sections:
        return []

    base_lines = sections[0]
    trans_lines: dict[float, str] = {}
    romaji_lines: dict[float, str] = {}

    if len(sections) >= 2:
        second = sections[1]
        if any(_is_romanized_line(text) for text in second.values()):
            romaji_lines = second
        else:
            trans_lines = second

    if len(sections) >= 3:
        third = sections[2]
        if romaji_lines:
            trans_lines = third
        else:
            romaji_lines = third

    parsed: list[LyricLine] = []
    for ts in sorted(base_lines):
        parsed.append(
            LyricLine(
                line=base_lines[ts],
                translation=trans_lines.get(ts),
                romaji=romaji_lines.get(ts),
            )
        )

    return parsed


driver = get_driver()


@driver.on_startup
def load_lyrics():
    global lyrics
    path = Path(cfg.fun_lyrics_directory)
    if not path.is_dir():
        logger.warning(f"歌词目录 {path} 不存在或不是一个目录，已跳过加载歌词")
        return

    # 解析目录下的所有.lrc文件为list[LyricLine]
    for lrc_file in path.glob("*.lrc"):
        try:
            parsed = _parse_lrc_file(lrc_file)
            if parsed:
                lyrics[lrc_file.stem] = parsed
        except Exception:
            logger.exception(f"加载歌词文件失败: {lrc_file}")

    # 解析txt文件，按行读取为list[str]
    for txt_file in path.glob("*.txt"):
        try:
            content = txt_file.read_text(encoding="utf-8")
            lyrics[txt_file.stem] = content
        except Exception:
            logger.exception(f"加载歌词文件失败: {txt_file}")

    logger.info(f"歌词加载完成，共加载 {len(lyrics)} 首")
