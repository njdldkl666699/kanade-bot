import asyncio
from asyncio import TimerHandle
from typing import Any

from nonebot import on_regex, require
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent, MessageSegment
from nonebot.matcher import Matcher
from nonebot.params import CommandArg, RegexDict
from nonebot.utils import run_sync

from kanade_bot.utils.parse import parse_arg_message

from .config import cfg
from .matcher import SessionId, dictionaries, games, hint, start_wordle, stop
from .wordle import DIC_LIST, GuessResult, Wordle

increment_crystal_maybe_init = None
if cfg.crystal_bonus_map:
    require("crystal")
    from kanade_bot.plugins.crystal import increment_crystal_maybe_init

timers: dict[str, TimerHandle] = {}
word_matchers: dict[str, type[Matcher]] = {}


def stop_game(session_id: str):
    if timer := timers.pop(session_id, None):
        timer.cancel()
    games.pop(session_id, None)
    if matcher := word_matchers.pop(session_id, None):
        matcher.destroy()


async def stop_game_timeout(matcher: Matcher, session_id: str):
    game = games.get(session_id, None)
    stop_game(session_id)
    if game:
        msg = "猜单词超时，游戏结束"
        if len(game.guessed_words) >= 1:
            msg += f"\n{game.result}"
        await matcher.send(msg)


def set_timeout(matcher: Matcher, session_id: str, timeout: float = 300):
    if timer := timers.get(session_id, None):
        timer.cancel()
    loop = asyncio.get_running_loop()
    timer = loop.call_later(
        timeout, lambda: asyncio.ensure_future(stop_game_timeout(matcher, session_id))
    )
    timers[session_id] = timer


def same_session(game_session_id: str):
    def _same_session(session_id: SessionId) -> bool:
        return session_id in games and session_id == game_session_id

    return _same_session


@start_wordle.handle()
async def _(matcher: Matcher, session_id: SessionId, arg_msg: Message = CommandArg()):
    args = parse_arg_message(
        arg_msg.extract_plain_text().strip(), {"length": int, "dictionary": str}
    )

    length: int = args["length"] or cfg.default_length
    mi = cfg.min_length
    ma = cfg.max_length
    if length < mi or length > ma:
        await matcher.finish(f"单词长度应在{mi}~{ma}之间")

    dictionary: str = args["dictionary"] or cfg.default_dictionary
    if dictionary not in DIC_LIST:
        await matcher.finish("支持的词典：" + ", ".join(DIC_LIST))

    game = Wordle.random_wordle(dictionary, length)
    games[session_id] = game
    set_timeout(matcher, session_id)

    word_matcher = on_regex(
        rf"^\s*(?P<word>[a-zA-Z]{{{length}}})\s*$",
        rule=same_session(session_id),
        block=True,
        priority=2,
    )
    word_matcher.append_handler(handle_word)
    word_matchers[session_id] = word_matcher

    words_count = Wordle.count_words_by_length(dictionary, length)
    message = Message(
        f"词典：{dictionary}，单词长度为{game.length}，共{words_count}词。\n"
        f"你有{game.rows}次机会猜出单词，请发送单词"
    )
    message += MessageSegment.image(await run_sync(game.draw)())
    await matcher.finish(message)


async def handle_word(
    matcher: Matcher,
    event: MessageEvent,
    session_id: SessionId,
    matched: dict[str, Any] = RegexDict(),
):
    game = games[session_id]
    set_timeout(matcher, session_id)

    word = str(matched["word"])
    result = game.guess(word)

    if result is None:
        await matcher.finish(MessageSegment.image(await run_sync(game.draw)()))
    elif result == GuessResult.DUPLICATE:
        await matcher.finish("你已经猜过这个单词了呢")
    elif result == GuessResult.ILLEGAL:
        await matcher.finish(f"你确定 {word} 是一个合法的单词吗？")

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
        message += " 猜出了单词！"

        if increment_crystal_maybe_init and (crystal := game.crystal_bonus) > 0:
            await increment_crystal_maybe_init(matcher, "onebot", user_id, crystal)
            message += f"\n你获得了 {crystal} 水晶奖励~"
    else:
        message += "很遗憾，没有人猜出来呢"
    message += f"\n{game.result}\n"
    message += MessageSegment.image(await run_sync(game.draw)())

    await matcher.finish(message)


@dictionaries.handle()
async def _():
    await dictionaries.finish("支持的词典：" + ", ".join(DIC_LIST))


@hint.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    set_timeout(matcher, session_id)

    hint = game.get_hint()
    if not hint.replace("*", ""):
        await matcher.finish("你还没有猜对过一个字母哦~再猜猜吧~")

    game.set_hinted_crystal_bonus()
    await matcher.finish(MessageSegment.image(await run_sync(game.draw_hint)(hint)))


@stop.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    stop_game(session_id)

    msg = "游戏已结束"
    if len(game.guessed_words) >= 1:
        msg += f"\n{game.result}"
    await matcher.finish(msg)
