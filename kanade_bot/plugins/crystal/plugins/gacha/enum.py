from enum import Enum


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
