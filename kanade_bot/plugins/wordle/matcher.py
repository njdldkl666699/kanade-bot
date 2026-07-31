from typing import Annotated

from nonebot import on_command, require
from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.params import Depends

from kanade_bot.utils.session import extract_session_info_sync

from .wordle import Wordle

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

games: dict[str, Wordle] = {}
"""正在进行的wordle游戏，key为会话id"""


def get_session_id(event: MessageEvent) -> str:
    return extract_session_info_sync(event).session_id


SessionId = Annotated[str, Depends(get_session_id)]


def game_is_running(session_id: SessionId) -> bool:
    return session_id in games


def game_not_running(session_id: SessionId) -> bool:
    return session_id not in games


start_wordle = on_command(
    "wordle",
    aliases={"猜单词"},
    rule=game_not_running,
    priority=2,
    block=True,
)
register_matcher(start_wordle, "猜单词")

dictionaries = on_command(
    "wordle_dictionaries",
    aliases={"猜单词词典", "词典", "词典列表"},
    priority=2,
    block=True,
)
register_matcher(dictionaries, "猜单词词典")

hint = on_command(
    "wordle_hint",
    aliases={"提示", "猜单词提示"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(hint, "猜单词提示")

stop = on_command(
    "wordle_stop",
    aliases={"结束", "结束游戏", "结束猜单词"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(stop, "结束猜单词")
