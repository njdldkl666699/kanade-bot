from datetime import time
from enum import Enum


class HandlerKeyEnum(Enum):
    """命令处理函数的唯一ID枚举"""

    CHAT = "聊天"
    REFRESH_WAIFU = "刷新老婆"
    RANDOM_WAIFU = "随机图"
    SUMMARIZE = "总结"
    GACHA = "抽卡"
    GACHA_10 = "十连抽"


class DaypartEnum(Enum):
    """每日签到的时间段枚举"""

    DAWN = "凌晨好"
    MORNING = "早上好"
    NOON = "中午好"
    AFTERNOON = "下午好"
    DUSK = "黄昏好"
    EVENING = "晚上好"
    NIGHT = "晚安"


# [start, end) 的时间段范围，跨越午夜的时间段需要拆分为两个范围
DAYPART_TIME_RANGES = {
    DaypartEnum.DAWN: [(time(0), time(6))],
    DaypartEnum.MORNING: [(time(5), time(11))],
    DaypartEnum.NOON: [(time(11), time(13))],
    DaypartEnum.AFTERNOON: [(time(13), time(17))],
    DaypartEnum.DUSK: [(time(16), time(19))],
    DaypartEnum.EVENING: [(time(18), time.max)],
    DaypartEnum.NIGHT: [(time(21), time.max), (time(0), time(2))],  # 跨越午夜的时间段
}
