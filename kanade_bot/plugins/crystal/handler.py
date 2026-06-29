import random
from datetime import datetime
from io import BytesIO
from warnings import deprecated

from nonebot.adapters import Bot, Event
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment

from kanade_bot.utils.common import get_platform_type

from .cache import check_in_cache
from .config import crystal_config, crystal_data
from .crystal import (
    check_user_crystal,
    finish_fail_consume,
    get_crystal,
    increment_crystal,
    succeed_consume,
)
from .enum import DAYPART_TIME_RANGES, DaypartEnum, HandlerKeyEnum
from .gacha import Card, gacha_draw_card, render_composed_card, render_gacha_10_cards
from .matcher import (
    check_in,
    check_ins,
    crystal_ranking,
    gacha,
    gacha_10,
    list_handler_consumes,
    my_crystal,
)


@check_in.handle()
@deprecated(
    "check_in.handle() is deprecated, please use check_ins[daypart].handle() instead.",
    category=DeprecationWarning,
)
async def _():
    daypart_commands = [f"/{daypart.value}" for daypart in DaypartEnum]
    await check_in.finish(
        f"请使用对应时间段的签到命令进行签到，包括：{'、'.join(daypart_commands)}"
    )


def get_current_dayparts() -> list[DaypartEnum]:
    """获取当前时间段列表"""
    dayparts: list[DaypartEnum] = []
    now = datetime.now().time()
    for daypart, time_ranges in DAYPART_TIME_RANGES.items():
        for start_time, end_time in time_ranges:
            if start_time <= now < end_time:
                dayparts.append(daypart)
                break  # 如果当前时间在某个时间段范围内，跳出内层循环
    return dayparts


for daypart, matcher in check_ins.items():

    @matcher.handle()
    async def _(event: Event, daypart=daypart):
        cfg = crystal_config.instance.check_in
        platform = get_platform_type(event)
        user_id = event.get_user_id()

        # 检查用户是否有水晶数据，如果没有，说明是首次使用此功能，赠送水晶并发送首次使用消息
        if user_id not in crystal_data.instance.get_by_platform(platform):
            increment_crystal(platform, user_id, cfg.first_use_bonus)
            template = random.choice(cfg.first_use_templates)
            message = template.format(first_use_bonus=cfg.first_use_bonus)
            await matcher.send(message)

        # 检查当前时间段是否与命令对应的时间段匹配
        current_dayparts = get_current_dayparts()
        if daypart not in current_dayparts:
            template = random.choice(cfg.wrong_daypart_templates)
            message = template.format(daypart=current_dayparts[0].value)
            await matcher.finish(message)

        check_in_dayparts = check_in_cache.get(platform, user_id)
        if check_in_dayparts is None:
            check_in_dayparts = set()

        # 检查签到次数，超过最大次数，返回达到最大次数的消息
        if len(check_in_dayparts) >= cfg.max_times:
            total_crystal = get_crystal(platform, user_id)
            template = random.choice(cfg.max_times_templates)
            message = template.format(daypart=daypart.value, total_crystal=total_crystal)
            await matcher.finish(message)

        # 用户已经在当前时间段签到过，返回已经签到的消息
        if daypart in check_in_dayparts:
            template = random.choice(cfg.already_templates)
            total_crystal = get_crystal(platform, user_id)
            message = template.format(daypart=daypart.value, total_crystal=total_crystal)
            await matcher.finish(message)

        # 进行签到
        crystal_earned = random.randint(cfg.min_crystal, cfg.max_crystal)
        increment_crystal(platform, user_id, crystal_earned)
        check_in_dayparts.add(daypart)
        # 改的是引用，但是可能原来为None，所以都重新设置缓存
        check_in_cache.set(platform, user_id, check_in_dayparts)
        # 返回签到成功消息
        template = random.choice(cfg.succeed_templates)
        message = template.format(daypart=daypart.value, crystal=crystal_earned)
        await matcher.finish(message)


@my_crystal.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    total_crystal = get_crystal(platform, user_id)
    await my_crystal.finish(f"你现在有 {total_crystal} 水晶。")


@list_handler_consumes.handle()
async def _():
    handler_consumes = crystal_config.instance.handler_consumes
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

    user_crystals = crystal_data.instance.get_by_platform(platform)
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


def _card_message_console(card: Card, bonus: int) -> str:
    """生成控制台消息"""
    messages = [
        f"你抽到了 {card.prefix} {card.character_name}！",
        f"稀有度: {card.card_rarity_type.value}",
        f"属性: {card.attr.value}",
        f"返还的水晶数: {bonus}",
    ]
    return "\n".join(messages)


def _card_message_onebot(card: Card, bonus: int) -> OneBotMessage:
    """生成 OneBot 消息"""
    card_image = render_composed_card(card)
    bytes_io = BytesIO()
    card_image.save(bytes_io, format="PNG")

    message = OneBotMessage()
    message += f"你抽到了 {card.prefix} {card.character_name}！\n"
    message += OneBotMessageSegment.image(bytes_io)
    message += f"\n返还的水晶数: {bonus}"
    return message


@gacha.handle()
async def _(bot: Bot, event: Event):
    key = HandlerKeyEnum.GACHA
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(gacha, key, platform, user_id)

    v = crystal_config.instance
    # 抽一次卡
    card = gacha_draw_card(v.gacha_cumulative_probabilities)
    succeed_consume(key, platform, user_id)

    # 返还水晶
    bonus = v.gacha_bonus_crystals.get(card.card_rarity_type, 0)
    increment_crystal(platform, user_id, bonus)

    if isinstance(bot, ConsoleBot):
        await gacha.finish(_card_message_console(card, bonus))
    elif isinstance(bot, OneBot):
        await gacha.finish(_card_message_onebot(card, bonus))


def _card_message_console_10(cards: list[Card], total_bonus: int) -> str:
    """生成控制台消息"""
    messages = ["你进行了十连抽！"]
    for i, card in enumerate(cards, start=1):
        messages.append(
            f"{i}. {card.prefix} {card.character_name} - 稀有度: {card.card_rarity_type.value}, 属性: {card.attr.value}"
        )
    messages.append(f"\n返还的水晶总数: {total_bonus}")
    return "\n".join(messages)


async def _card_message_onebot_10(cards: list[Card], total_bonus: int) -> OneBotMessage:
    """生成 OneBot 消息"""
    message = OneBotMessage()
    cards_image = await render_gacha_10_cards(cards)
    message += OneBotMessageSegment.image(cards_image)
    message += f"\n返还的水晶总数: {total_bonus}"
    return message


@gacha_10.handle()
async def _(bot: Bot, event: Event):
    key = HandlerKeyEnum.GACHA_10
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(gacha_10, key, platform, user_id)

    # 抽十次卡
    cards: list[Card] = []
    v = crystal_config.instance
    pity = random.randint(0, 9)
    for i in range(10):
        if i == pity:
            card = gacha_draw_card(v.gacha_pity_cumulative_probabilities)
        else:
            card = gacha_draw_card(v.gacha_cumulative_probabilities)
        cards.append(card)
    succeed_consume(key, platform, user_id)

    # 返还水晶
    total_bonus = sum(v.gacha_bonus_crystals.get(card.card_rarity_type, 0) for card in cards)
    increment_crystal(platform, user_id, total_bonus)

    if isinstance(bot, ConsoleBot):
        await gacha_10.finish(_card_message_console_10(cards, total_bonus))
    elif isinstance(bot, OneBot):
        message = await _card_message_onebot_10(cards, total_bonus)
        await gacha_10.finish(message)
