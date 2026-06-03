import random

from nonebot.adapters import Bot, Event
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot

from kanade_bot.plugins.crystal.cache import checkInCache
from kanade_bot.plugins.crystal.crystal import get_crystal, increment_crystal
from kanade_bot.utils.common import get_platform_type

from .config import crystal_config_ptr, crystal_data_ptr
from .matcher import check_in, crystal_ranking, list_handler_consumes, my_crystal


@check_in.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()
    cfg = crystal_config_ptr.v

    if checkInCache.get(platform, user_id):
        total_crystal = get_crystal(platform, user_id)
        template = random.choice(cfg.check_in_failed_templates)
        failed_message = template.format(total_crystal=total_crystal)
        await check_in.finish(failed_message)

    crystal_earned = random.randint(cfg.check_in_min, cfg.check_in_max)
    increment_crystal(platform, user_id, crystal_earned)
    checkInCache.set(platform, user_id, True)

    template = random.choice(cfg.check_in_succeed_templates)
    message = template.format(crystal=crystal_earned)
    await check_in.finish(message)


@my_crystal.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    total_crystal = get_crystal(platform, user_id)
    await my_crystal.finish(f"你现在有 {total_crystal} 水晶。")


@list_handler_consumes.handle()
async def _():
    handler_consumes = crystal_config_ptr.v.handler_consumes
    if not handler_consumes:
        await list_handler_consumes.finish("当前没有命令水晶消耗设置。")

    message_lines = ["💎当前命令水晶消耗设置："]
    for handler_key, consume in handler_consumes.items():
        message_lines.append(f"- {handler_key.value}: {consume}")
    message = "\n".join(message_lines)
    await list_handler_consumes.finish(message)


RANK_EMOJIS = {1: "🥇", 2: "🥈", 3: "🥉"}


@crystal_ranking.handle()
async def _(bot: Bot, event: Event):
    platform = get_platform_type(event)

    user_crystals = crystal_data_ptr.v.get_by_platform(platform)
    if not user_crystals:
        await crystal_ranking.finish("当前没有用户水晶数据。")

    message_lines = ["🏆水晶排行榜："]
    sorted_users = sorted(user_crystals.items(), key=lambda x: x[1], reverse=True)
    for index, (user_id, crystal) in enumerate(sorted_users[:10], start=1):
        rank_emoji = RANK_EMOJIS.get(index, "-")

        nickname = user_id
        if isinstance(bot, ConsoleBot):
            user = await bot.get_user(user_id)
            nickname = user.nickname
        elif isinstance(bot, OneBot):
            user = await bot.get_stranger_info(user_id=int(user_id))
            nickname = user["nickname"]

        message_lines.append(f"{rank_emoji} {nickname}: {crystal}")

    message = "\n".join(message_lines)
    await crystal_ranking.finish(message)
