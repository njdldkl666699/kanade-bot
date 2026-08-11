from nonebot import on_command, on_regex, require

from kanade_bot.plugins.wordle.util import SessionId

from .handle import Handle

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

games: dict[str, Handle] = {}
"""正在进行的handle游戏，key为会话id"""


def game_is_running(session_id: SessionId) -> bool:
    return session_id in games


def game_not_running(session_id: SessionId) -> bool:
    return session_id not in games


start_handle = on_command(
    "handle",
    aliases={"猜成语"},
    rule=game_not_running,
    priority=2,
    block=True,
)
register_matcher(start_handle, "开始猜成语")

matcher_idiom = on_regex(
    r"^(?P<idiom>[\u4e00-\u9fa5]{4})$",
    rule=game_is_running,
    block=True,
    priority=2,
)

hint = on_command(
    "handle_hint",
    aliases={"提示", "猜成语提示"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(hint, "猜成语提示")

stop = on_command(
    "handle_stop",
    aliases={"结束", "结束游戏", "结束猜成语"},
    rule=game_is_running,
    priority=2,
    block=True,
)
register_matcher(stop, "结束猜成语")
