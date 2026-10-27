from datetime import datetime
from typing import Literal

from nonebot import require
from nonebot.adapters import Event
from pydantic_ai.usage import RunUsage

from kanade_bot.utils.billing import UsageCallback, compute_token_cost, is_peak_hours
from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.session import extract_session_info_sync

from .config import cfg, chat_configs

require("crystal")
from kanade_bot.plugins.crystal import consume_crystal

type BanType = Literal["user", "group"]


def group_id_of(event: Event) -> str | None:
    """从群消息事件提取群 ID；非群消息返回 None"""
    return extract_session_info_sync(event).group_id


def get_ban_list(ban_type: BanType, platform: PlatformType):
    """取指定平台对应类型的黑名单集合"""
    config = chat_configs.instance.get_by_platform(platform)

    if ban_type == "user":
        return config.banned_users
    if ban_type == "group":
        return config.banned_groups
    return set()


def is_banned(id: str, ban_type: BanType, platform: PlatformType) -> bool:
    return id in get_ban_list(ban_type, platform)


def make_turn_biller(
    platform: PlatformType | None, user_id: str | None, turn_start: datetime
) -> UsageCallback:
    """构造轮次计费回调：有文本产出才扣费（扣费允许至负数）

    被动回复与定时任务主动唤醒共用；峰谷按轮次开始时刻判定。
    """

    def bill(usage: RunUsage, produced: bool) -> None:
        if not produced or not platform or not user_id:
            return
        cost = compute_token_cost(usage, cfg.billing, peak=is_peak_hours(turn_start))
        consume_crystal(platform, user_id, cost)

    return bill
