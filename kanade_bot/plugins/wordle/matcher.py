from nonebot import on_command, require

from .wordle import Wordle

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

games: dict[str, Wordle] = {}
"""正在进行的wordle游戏，key为会话id"""


def game_is_running(session_id: str) -> bool:
    return session_id in games


def game_not_running(session_id: str) -> bool:
    return session_id not in games


def same_session(game_session_id: str):
    def _same_session(session_id: str) -> bool:
        return session_id in games and session_id == game_session_id

    return _same_session


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
