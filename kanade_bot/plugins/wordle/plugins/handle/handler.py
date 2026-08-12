import asyncio
from asyncio import TimerHandle
from typing import Any

from nonebot import require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import RegexDict
from nonebot.utils import run_sync

from kanade_bot.plugins.wordle.util import GuessResult, SessionId

from .config import cfg
from .handle import Handle
from .matcher import games, hint, matcher_idiom, start_handle, stop

increment_crystal_maybe_init = None
if cfg.crystal_bonus:
    require("crystal")
    from kanade_bot.plugins.crystal import increment_crystal_maybe_init

timers: dict[str, TimerHandle] = {}


def stop_game(session_id: str):
    if timer := timers.pop(session_id, None):
        timer.cancel()
    games.pop(session_id, None)


async def stop_game_timeout(matcher: Matcher, session_id: str):
    game = games.get(session_id, None)
    stop_game(session_id)
    if game:
        msg = "猜成语超时，游戏结束。"
        if len(game.guessed_idiom) >= 1:
            msg += f"\n{game.result}"
        await matcher.finish(msg)


def set_timeout(matcher: Matcher, session_id: str, timeout: float = 300):
    if timer := timers.get(session_id, None):
        timer.cancel()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(
        timeout, lambda: asyncio.ensure_future(stop_game_timeout(matcher, session_id))
    )
    timers[session_id] = timer


@start_handle.handle()
async def _(matcher: Matcher, session_id: SessionId):
    is_strict = cfg.strict_mode
    game = Handle.random_handle(strict=is_strict)

    games[session_id] = game
    set_timeout(matcher, session_id)

    message = Message(
        f"你有{game.times}次机会猜一个四字成语，"
        + ("发送有效成语以参与游戏。" if is_strict else "发送任意四字词语以参与游戏。")
    )
    message += MessageSegment.image(await run_sync(game.draw)())
    await matcher.finish(message)


@hint.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    set_timeout(matcher, session_id)
    game.set_hinted_crystal_bonus()
    await matcher.finish(MessageSegment.image(await run_sync(game.draw_hint)()))


@stop.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    stop_game(session_id)

    msg = "游戏已结束"
    if len(game.guessed_idiom) >= 1:
        msg += f"\n{game.result}"
    await matcher.finish(msg)


@matcher_idiom.handle()
async def _(
    matcher: Matcher,
    event: MessageEvent,
    session_id: SessionId,
    matched: dict[str, Any] = RegexDict(),
):
    game = games[session_id]
    set_timeout(matcher, session_id)

    idiom = str(matched["idiom"])
    result = game.guess(idiom)

    if result is None:
        await matcher.finish(MessageSegment.image(await run_sync(game.draw)()))
    elif result == GuessResult.DUPLICATE:
        await matcher.finish("你已经猜过这个成语了呢")
    elif result == GuessResult.ILLEGAL:
        await matcher.finish(f"你确定“{idiom}”是个成语吗？")

    # WIN or LOSS
    stop_game(session_id)

    message = Message()
    if result == GuessResult.WIN:
        user_id = event.get_user_id()

        message += "恭喜"
        user_segment = "你"
        if isinstance(event, GroupMessageEvent):
            user_segment = MessageSegment.at(user_id)
        message += user_segment
        message += " 猜出了成语！"

        if increment_crystal_maybe_init and (crystal := game.crystal_bonus) > 0:
            await increment_crystal_maybe_init(matcher, "onebot", user_id, crystal)
            message += f"\n你获得了 {crystal} 水晶奖励~"
    else:
        message += "很遗憾，没有人猜出来呢"
    message += f"\n{game.result}\n"
    message += MessageSegment.image(await run_sync(game.draw)())

    await matcher.finish(message)
