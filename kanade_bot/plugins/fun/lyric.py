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

# 歌词文件列表
lyric_files: set[Path] = set()


def query_lyric_files(query: str | None = None) -> set[Path] | None:
    """查询歌词列表中包含query的歌词文件，返回匹配的歌词文件列表"""
    if not lyric_files:
        raise ValueError("歌词列表未加载")
    if not query:
        return lyric_files

    query = query.lower()
    matched_files = {file for file in lyric_files if query in file.name.lower()}
    return matched_files if matched_files else None


def get_random_lyric(
    query: str | None = None, length: int | None = None
) -> tuple[Path, Lyric] | None:
    """查询歌词列表中包含query的歌曲，随机选择一首，并返回歌曲文件和随机裁剪的歌词片段"""
    if length is None:
        length = cfg.fun_lyric_default_length

    filtered_files = query_lyric_files(query)
    if not filtered_files:
        return None

    # 随机选择歌曲并按需读取歌词，若遇到失效路径则跳过
    filtered_files = list(filtered_files)
    random.shuffle(filtered_files)
    for selected_file in filtered_files:
        lyric = _read_lyric_file(selected_file)
        if lyric is not None:
            break
    else:
        return None

    # 如果歌词是字符串，直接返回
    if isinstance(lyric, str):
        return selected_file, lyric

    # 随机裁剪歌词片段
    if length >= len(lyric):
        return selected_file, lyric
    start_index = random.randint(0, len(lyric) - length)
    return selected_file, lyric[start_index : start_index + length]


def add_lyric_txt(song_name: str, lyric_txt: str):
    """添加歌曲歌词到歌词列表，song_name为歌曲名，lyric为txt歌词文本"""
    song_name = song_name.strip()
    if not song_name:
        raise ValueError("歌曲名不能为空")
    lyric_txt = lyric_txt.strip()
    if not lyric_txt:
        raise ValueError("歌词内容不能为空")

    lyric_dir = Path(cfg.fun_lyrics_dir_path)
    lyric_dir.mkdir(parents=True, exist_ok=True)

    normalized_song_name = _normalize_song_name(song_name)
    lyric_file = lyric_dir / f"{normalized_song_name}.txt"
    lyric_file.write_text(lyric_txt, encoding="utf-8")
    lyric_files.add(lyric_file)

    logger.info(f"已添加歌词: {song_name} -> {normalized_song_name} ({lyric_file})")


def remove_song_lyric(song_name: str):
    """从歌词列表中移除歌曲"""
    song_name = song_name.strip()
    if not song_name:
        raise ValueError("歌曲名不能为空")

    normalized_song_name = _normalize_song_name(song_name)
    lyric_dir = Path(cfg.fun_lyrics_dir_path)
    txt_file = lyric_dir / f"{normalized_song_name}.txt"
    lrc_file = lyric_dir / f"{normalized_song_name}.lrc"

    for lyric_file in (txt_file, lrc_file):
        if lyric_file.exists():
            lyric_file.unlink()
            lyric_files.discard(lyric_file)

    logger.info(f"已删除歌词: {song_name} -> {normalized_song_name}")


def _read_lyric_file(lyric_file: Path) -> Lyric | None:
    if not lyric_file.exists():
        lyric_files.discard(lyric_file)
        return None

    try:
        if lyric_file.suffix.lower() == ".lrc":
            parsed = _parse_lrc_file(lyric_file)
            return parsed if parsed else None

        if lyric_file.suffix.lower() == ".txt":
            return lyric_file.read_text(encoding="utf-8")

        logger.warning(f"不支持的歌词文件类型: {lyric_file}")
        return None
    except Exception:
        logger.exception(f"读取歌词文件失败: {lyric_file}")
        return None


TIMESTAMP_PATTERN = re.compile(r"\[(\d{1,2}:\d{2}(?:\.\d{1,3})?)\]")
METADATA_PATTERN = re.compile(r"^\[[a-zA-Z]+:.*\]$")
INVALID_FILENAME_CHARS_PATTERN = re.compile(r"[\\/*?\"<>|\n\r\t]")


def _normalize_song_name(song_name: str) -> str:
    """将歌曲名标准化为安全文件名，避免路径分隔符导致写入失败。"""
    normalized = INVALID_FILENAME_CHARS_PATTERN.sub("", song_name).strip().strip(".")
    if not normalized:
        raise ValueError("歌曲名仅包含非法字符，无法保存")
    return normalized


def _timestamp_to_seconds(timestamp: str) -> float:
    minutes_str, seconds_str = timestamp.split(":", maxsplit=1)
    return int(minutes_str) * 60 + float(seconds_str)


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
        trans_lines = sections[1]
    if len(sections) >= 3:
        romaji_lines = sections[2]

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
    global lyric_files
    path = Path(cfg.fun_lyrics_dir_path)
    if not path.is_dir():
        logger.warning(f"歌词目录 {path} 不存在或不是一个目录，已跳过加载歌词")
        return

    lyric_files = set(path.glob("*.lrc")) | set(path.glob("*.txt"))
    logger.info(f"歌词加载完成，共加载 {len(lyric_files)} 首")
