"""主动回复时段概率窗口匹配"""

from datetime import datetime

from kanade_bot.utils.billing import is_non_workday
from kanade_bot.utils.common import asia_shanghai_now

from ...config import TimeWindow, cfg


def time_in_window(current, start, end) -> bool:
    """时刻是否落在 `[start, end)` 半开区间内，支持跨零点（23:00 → 07:00）"""
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def match_time_window(at: datetime | None = None) -> TimeWindow | None:
    """命中的时段窗口：多个命中取列表中最先声明者（自上而下即优先级）

    都不命中时返回 None（时段外每批必送，概率视为 1.0）。
    """
    at = at or asia_shanghai_now()
    current = at.time()
    for window in cfg.auto_reply.time_windows:
        if window.weekdays_only and is_non_workday(at):
            continue
        if time_in_window(current, window.start, window.end):
            return window
    return None


def window_probability(at: datetime | None = None) -> tuple[float, TimeWindow | None]:
    """当前时刻的送审概率与命中窗口；无窗口命中时概率为 1.0"""
    window = match_time_window(at)
    return (window.probability if window else 1.0), window


def describe_window(window: TimeWindow | None) -> str:
    """窗口的简短描述（日志与统计用）"""
    if window is None:
        return "无窗口命中"
    desc = f"{window.start:%H:%M}-{window.end:%H:%M}"
    if window.weekdays_only:
        desc += " 工作日"
    return desc
