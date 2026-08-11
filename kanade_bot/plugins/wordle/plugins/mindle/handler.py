import asyncio
from asyncio import TimerHandle
from functools import lru_cache

from nonebot import require
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.utils import run_sync

from kanade_bot.plugins.wordle.util import GuessResult, SessionId

from .config import cfg
from .matcher import games, guess_item, hint, start_mindle, stop
from .mindle import Mindle, MindleDataError, RecipeBook

increment_crystal_maybe_init = None
if cfg.crystal_bonus:
    require("crystal")
    from kanade_bot.plugins.crystal import increment_crystal_maybe_init

timers: dict[str, TimerHandle] = {}


@lru_cache(maxsize=1)
def get_recipe_book() -> RecipeBook:
    return RecipeBook.load(
        cfg.recipes_dir_path,
        cfg.lang_file_path,
        cfg.render_items_dir_path,
        cfg.background_image_path,
    )


def stop_game(session_id: str):
    if timer := timers.pop(session_id, None):
        timer.cancel()
    games.pop(session_id, None)


async def stop_game_timeout(matcher: Matcher, session_id: str):
    game = games.get(session_id)
    stop_game(session_id)
    if game:
        await matcher.send(f"猜配方超时，游戏结束。\n{game.result}")


def set_timeout(matcher: Matcher, session_id: str, timeout: float = 300):
    if timer := timers.get(session_id):
        timer.cancel()
    loop = asyncio.get_running_loop()
    timers[session_id] = loop.call_later(
        timeout,
        lambda: asyncio.ensure_future(stop_game_timeout(matcher, session_id)),
    )


@start_mindle.handle()
async def _(matcher: Matcher, session_id: SessionId):
    try:
        book = get_recipe_book()
    except (MindleDataError, OSError, ValueError) as exc:
        await matcher.finish(f"猜配方资源加载失败：{exc}")

    game = Mindle.random_mindle(book, cfg.max_attempts)
    games[session_id] = game
    set_timeout(matcher, session_id)

    message = Message(
        f"猜配方开始！你有 {game.max_attempts} 次机会，请使用 /猜 <物品名称> 猜测 Minecraft 物品。"
    )
    message += MessageSegment.image(await run_sync(game.draw)())
    await matcher.finish(message)


def _suggestion_message(event: MessageEvent, item_name: str, suggestions: list[str]) -> Message:
    message = Message()
    if isinstance(event, GroupMessageEvent):
        message += MessageSegment.at(event.get_user_id())
    else:
        message += "你"
    if suggestions:
        message += " 未匹配到精确名称，你是不是想猜：\n"
        message += "\n".join(f"- {name}" for name in suggestions)
    else:
        message += f" 未找到与“{item_name}”相近的可合成物品"
    return message


@guess_item.handle()
async def _(
    matcher: Matcher,
    event: MessageEvent,
    session_id: SessionId,
    message: Message = CommandArg(),
):
    game = games[session_id]
    set_timeout(matcher, session_id)
    item_name = message.extract_plain_text().strip()
    recipe = game.book.get(item_name)
    if recipe is None:
        await matcher.finish(
            _suggestion_message(event, item_name, game.book.suggestions(item_name))
        )

    result = game.guess(recipe)
    if result == GuessResult.DUPLICATE:
        await matcher.finish("这个物品已经猜过了呢")

    if result is None:
        image = MessageSegment.image(await run_sync(game.draw)())
        await matcher.finish(
            Message(f"第 {len(game.guessed_item_ids)}/{game.max_attempts} 次猜测") + image
        )

    stop_game(session_id)
    message = Message()
    if result == GuessResult.WIN:
        user_id = event.get_user_id()
        message += "恭喜"
        message += MessageSegment.at(user_id) if isinstance(event, GroupMessageEvent) else "你"
        message += " 猜出了配方！"
        message += f"\n{game.result}\n"
        message += MessageSegment.image(await run_sync(game.draw)(game.answer))

        if increment_crystal_maybe_init and cfg.crystal_bonus > 0:
            await increment_crystal_maybe_init(matcher, "onebot", user_id, cfg.crystal_bonus)
        message += f"\n你获得了 {cfg.crystal_bonus} 水晶奖励~"
    else:
        message += "很遗憾，没有人猜出来呢"
        message += f"\n{game.result}\n"
        message += MessageSegment.image(await run_sync(game.draw)(game.answer, states=()))
    await matcher.finish(message)


@hint.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    set_timeout(matcher, session_id)
    recipe_hint = game.get_hint()
    if recipe_hint is None:
        await matcher.finish("暂时无法获取这个配方的原料提示")

    message = Message(
        f"配方原料提示：\n中文名：{recipe_hint.item_name}\n物品 ID：{recipe_hint.item_id}\n"
    )
    message += MessageSegment.image(recipe_hint.image_path)
    await matcher.finish(message)


@stop.handle()
async def _(matcher: Matcher, session_id: SessionId):
    game = games[session_id]
    stop_game(session_id)
    message = Message()
    message += "游戏已结束\n"
    message += f"{game.result}\n"
    message += MessageSegment.image(await run_sync(game.draw)(game.answer, states=()))
    await matcher.finish(message)
