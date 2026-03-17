from pathlib import Path
from typing import Literal

from nonebot import get_plugin_config
from pydantic import BaseModel
from .config import Config

cfg = get_plugin_config(Config)


class BanList(BaseModel):
    """聊天拉黑列表，包含Console和OneBot两部分"""

    console: set[str]
    onebot: set[str]


def get_ban_list():
    return BanList.model_validate_json(Path(cfg.chat_ban_path).read_text(encoding="utf-8"))


def is_user_banned(user_id: str, platform: Literal["console", "onebot"]) -> bool:
    ban_list = get_ban_list()
    if platform == "console":
        return user_id in ban_list.console
    elif platform == "onebot":
        return user_id in ban_list.onebot
    else:
        raise ValueError("Invalid platform. Must be 'console' or 'onebot'.")


def add_user_to_ban_list(user_id: str, platform: Literal["console", "onebot"]):
    ban_list = get_ban_list()
    if platform == "console":
        ban_list.console.add(user_id)
    elif platform == "onebot":
        ban_list.onebot.add(user_id)
    else:
        raise ValueError("Invalid platform. Must be 'console' or 'onebot'.")
    Path(cfg.chat_ban_path).write_text(
        ban_list.model_dump_json(indent=4, ensure_ascii=False), encoding="utf-8"
    )


def remove_user_from_ban_list(user_id: str, platform: Literal["console", "onebot"]):
    ban_list = get_ban_list()
    if platform == "console":
        ban_list.console.discard(user_id)
    elif platform == "onebot":
        ban_list.onebot.discard(user_id)
    else:
        raise ValueError("Invalid platform. Must be 'console' or 'onebot'.")
    Path(cfg.chat_ban_path).write_text(
        ban_list.model_dump_json(indent=4, ensure_ascii=False), encoding="utf-8"
    )
