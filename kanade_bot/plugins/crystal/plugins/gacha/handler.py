import random
from io import BytesIO

from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment

from kanade_bot.plugins.crystal import (
    check_user_crystal,
    finish_fail_consume,
    increment_crystal,
    succeed_consume,
)
from kanade_bot.plugins.crystal.enum import HandlerKeyEnum
from kanade_bot.utils.common import get_platform_type

from .config import gacha_config
from .gacha import Card, gacha_draw_card, render_composed_card, render_gacha_10_cards
from .matcher import gacha, gacha_10


@gacha.handle()
async def _(event: Event):
    key = HandlerKeyEnum.GACHA
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(gacha, key, platform, user_id)

    v = gacha_config.instance
    # 抽一次卡
    card = gacha_draw_card(v.cumulative_probabilities)
    succeed_consume(key, platform, user_id)

    # 返还水晶
    bonus = v.bonus_crystals.get(card.card_rarity_type, 0)
    increment_crystal(platform, user_id, bonus)

    if isinstance(event, ConsoleEvent):
        messages = [
            f"你抽到了 {card.prefix} {card.character_name}！",
            f"稀有度: {card.card_rarity_type.value}",
            f"属性: {card.attr.value}",
            f"返还的水晶数: {bonus}",
        ]
        await gacha.finish("\n".join(messages))
    elif isinstance(event, OneBotEvent):
        card_image = render_composed_card(card)
        bytes_io = BytesIO()
        card_image.save(bytes_io, format="PNG")

        message = OneBotMessage()
        message += OneBotMessageSegment.at(user_id)
        message += f"\n你抽到了 {card.prefix} {card.character_name}！\n"
        message += OneBotMessageSegment.image(bytes_io)
        message += f"返还的水晶数: {bonus}"
        await gacha.finish(message)


@gacha_10.handle()
async def _(event: Event):
    key = HandlerKeyEnum.GACHA_10
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(gacha_10, key, platform, user_id)

    # 抽十次卡
    cards: list[Card] = []
    v = gacha_config.instance
    pity = random.randint(0, 9)
    for i in range(10):
        if i == pity:
            card = gacha_draw_card(v.pity_cumulative_probabilities)
        else:
            card = gacha_draw_card(v.cumulative_probabilities)
        cards.append(card)
    succeed_consume(key, platform, user_id)

    # 返还水晶
    total_bonus = sum(v.bonus_crystals.get(card.card_rarity_type, 0) for card in cards)
    increment_crystal(platform, user_id, total_bonus)

    if isinstance(event, ConsoleEvent):
        messages = ["你进行了十连抽！"]
        for i, card in enumerate(cards, start=1):
            messages.append(
                f"{i}. {card.prefix} {card.character_name} - 稀有度: {card.card_rarity_type.value}, 属性: {card.attr.value}"
            )
        messages.append(f"返还的水晶总数: {total_bonus}")
        await gacha_10.finish("\n".join(messages))
    elif isinstance(event, OneBotEvent):
        cards_image = await render_gacha_10_cards(cards)
        bytes_io = BytesIO()
        cards_image.save(bytes_io, format="PNG")

        message = OneBotMessage()
        message += OneBotMessageSegment.at(user_id)
        message += OneBotMessageSegment.image(bytes_io)
        message += f"返还的水晶总数: {total_bonus}"
        await gacha_10.finish(message)
