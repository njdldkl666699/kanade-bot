from nonebot import on_command, require

from kanade_bot.plugins.wordle.util import SessionId

from .mindle import Mindle

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

games: dict[str, Mindle] = {}


def game_is_running(session_id: SessionId) -> bool:
    return session_id in games


def game_not_running(session_id: SessionId) -> bool:
    return session_id not in games


start_mindle = on_command(
    "mindle",
    aliases={"开始猜配方"},
    rule=game_not_running,
    priority=2,
    block=True,
)
register_matcher(start_mindle, "猜配方")

guess_item = on_command(
    "mindle_guess",
    aliases={"猜配方", "猜"},
    rule=game_is_running,
    priority=2,
    block=True,
)

stop = on_command(
    "mindle_stop",
    aliases={"结束猜配方", "结束"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(stop, "结束猜配方")

hint = on_command(
    "mindle_hint",
    aliases={"猜配方提示", "提示"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(hint, "猜配方提示")
