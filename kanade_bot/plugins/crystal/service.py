import random

from nonebot.adapters import Event

from kanade_bot.plugins.crystal.cache import UserDailyCheckInCache
from kanade_bot.plugins.crystal.crystal import get_crystal, increment_crystal
from kanade_bot.utils.common import get_platform_type

from .config import crystal_config as cfg
from .matcher import check_in, my_crystal


@check_in.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if UserDailyCheckInCache.get(platform, user_id):
        total_crystal = get_crystal(platform, user_id)
        template = random.choice(cfg.check_in_failure_templates)
        check_in_failure_message = template.format(total_crystal=total_crystal)
        await check_in.finish(check_in_failure_message)

    crystal_earned = random.randint(cfg.check_in_crystal_min, cfg.check_in_crystal_max)
    increment_crystal(platform, user_id, crystal_earned)
    UserDailyCheckInCache.set(platform, user_id, True)


@my_crystal.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    total_crystal = get_crystal(platform, user_id)
    await my_crystal.finish(f"你现在有 {total_crystal} 水晶。")
