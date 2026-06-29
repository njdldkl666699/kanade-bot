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
    DaypartEnum.DAWN: [(time(2), time(6))],
    DaypartEnum.MORNING: [(time(6), time(9))],
    DaypartEnum.NOON: [(time(9), time(12))],
    DaypartEnum.AFTERNOON: [(time(12), time(16))],
    DaypartEnum.DUSK: [(time(16), time(18))],
    DaypartEnum.EVENING: [(time(18), time(21))],
    DaypartEnum.NIGHT: [(time(21), time.max), (time(0), time(2))],  # 跨越午夜的时间段
}


class RarityEnum(Enum):
    """卡牌稀有度枚举"""

    ONE = "rarity_1"
    TWO = "rarity_2"
    THREE = "rarity_3"
    BIRTHDAY = "rarity_birthday"
    FOUR = "rarity_4"

    @property
    def card_frame_L(self) -> str:
        """获取卡牌大图的边框图片文件名

        图片尺寸：940x530
        """
        return f"cardFrame_L_{self.value}.png"

    @property
    def can_train(self) -> bool:
        """判断该稀有度的卡牌是否可以特训"""
        return self in (RarityEnum.THREE, RarityEnum.FOUR)

    def rarity_icon(self, trained: bool = False) -> tuple[str, int]:
        """获取卡牌稀有度图标文件名和数量

        图片尺寸：72x70
        """
        if self == RarityEnum.BIRTHDAY:
            return "rare_birthday.png", 1
        num = int(self.value.replace("rarity_", ""))
        if trained and self.can_train:
            return "rare_star_after_training.png", num
        return "rare_star_normal.png", num


class AttrEnum(Enum):
    """卡牌属性枚举"""

    COOL = "cool"
    CUTE = "cute"
    PURE = "pure"
    HAPPY = "happy"
    MYSTERIOUS = "mysterious"

    @property
    def icon_attribute_88(self) -> str:
        """获取卡牌属性图标文件名

        图片尺寸：88x92
        """
        return f"icon_attribute_{self.value}_88.png"


CHARACTERS = [
    "星乃一歌",
    "天马咲希",
    "望月穗波",
    "日野森志步",
    "花里实乃理",
    "桐谷遥",
    "桃井爱莉",
    "日野森雫",
    "小豆泽心羽",
    "白石杏",
    "东云彰人",
    "青柳冬弥",
    "天马司",
    "凤笑梦",
    "草薙宁宁",
    "神代类",
    "宵崎奏",
    "朝比奈真冬",
    "东云绘名",
    "晓山瑞希",
    "初音未来",
    "镜音铃",
    "镜音连",
    "巡音流歌",
    "MEIKO",
    "KAITO",
]
