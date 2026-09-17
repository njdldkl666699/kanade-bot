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
    IMAGE_GENERATION = "文生图"
    IMAGE_EDIT = "图片编辑"
    VIDEO_GENERATION = "视频生成"


class DaypartEnum(Enum):
    """每日签到的时间段枚举"""

    PREDAWN = "凌晨好"
    DAWN = "清晨好"
    MORNING = "早上好"
    NOON = "中午好"
    AFTERNOON = "下午好"
    DUSK = "黄昏好"
    EVENING = "晚上好"
    MIDNIGHT = "午夜好"
    NIGHT = "晚安"


# [start, end) 的时间段范围，跨越午夜的时间段需要拆分为两个范围
DAYPART_TIME_RANGES = {
    DaypartEnum.PREDAWN: [(time(1), time(5))],
    DaypartEnum.DAWN: [(time(4), time(8))],
    DaypartEnum.MORNING: [(time(7), time(11))],
    DaypartEnum.NOON: [(time(10), time(14))],
    DaypartEnum.AFTERNOON: [(time(13), time(17))],
    DaypartEnum.DUSK: [(time(16), time(20))],
    DaypartEnum.EVENING: [(time(19), time(23))],
    DaypartEnum.MIDNIGHT: [(time(22), time.max), (time(0), time(2))],
    DaypartEnum.NIGHT: [(time(20), time.max), (time(0), time(4))],
}
