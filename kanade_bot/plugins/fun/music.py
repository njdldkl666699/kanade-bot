import json
import random
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger
from pydantic import BaseModel
from pydantic.alias_generators import to_camel

from .config import Config

cfg = get_plugin_config(Config)


class MusicQuality(BaseModel):
    type: str
    size: str | None = None
    hash: str | None = None


class MusicMeta(BaseModel):
    song_id: str | int
    album_name: str
    pic_url: str | None
    qualitys: list[MusicQuality] | None = None

    model_config = {"alias_generator": to_camel, "validate_by_name": True}


class Music(BaseModel):
    name: str
    singer: str
    source: str
    interval: str
    meta: MusicMeta

    @property
    def pretty_string(self) -> str:
        album_name = self.meta.album_name.strip()

        return (
            f"🎵 {self.name} - {self.singer}\n"
            + (f"💿 专辑: {album_name}\n" if album_name else "")
            + f"🕒 时长: {self.interval}"
        )


class MusicList(BaseModel):
    name: str
    list: list[Music]


list_cache: dict[str, MusicList] = {}


def get_music_list_names() -> list[str]:
    if not list_cache:
        raise ValueError("音乐列表未加载")
    return list(list_cache.keys())


def get_random_music(list_query: str | None = None) -> tuple[str, Music]:
    if not list_cache:
        raise ValueError("音乐列表未加载")

    if not list_query:
        music_list = random.choice(list(list_cache.values()))
    else:
        # 简单的模糊匹配
        matched_lists = [lst for lst in list_cache.values() if list_query in lst.name]
        if not matched_lists:
            raise ValueError(f"未找到匹配的音乐列表: {list_query}")
        music_list = random.choice(matched_lists)

    music = random.choice(music_list.list)
    return music_list.name, music


driver = get_driver()


@driver.on_startup
def load_music_list():
    global list_cache
    path = Path(cfg.fun_music_list_file_path)
    lists = json.load(path.open("r", encoding="utf-8"))["data"]
    logger.info(f"正在加载音乐列表，路径: {path.absolute()}, 共 {len(lists)} 个列表")
    for list in lists:
        name = list["name"]
        if name not in {"list__name_default", "list__name_love"}:
            list_cache[name] = MusicList.model_validate(list)
            logger.info(f"加载音乐列表: {name}, 共 {len(list_cache[name].list)} 首歌")
