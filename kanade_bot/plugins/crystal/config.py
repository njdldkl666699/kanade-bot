from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel

from kanade_bot.utils.common import PlatformType

from .enum import HandlerKeyEnum, RarityEnum

require("nonebot_plugin_localstore")
require("model_updater")

from nonebot_plugin_localstore import (
    get_plugin_cache_file,
    get_plugin_config_dir,
    get_plugin_config_file,
    get_plugin_data_file,
)

from kanade_bot.plugins.model_updater import load_register_model_from_file


class GachaScopedConfig(BaseModel):
    """抽卡系统配置"""

    member_small_dir: str = "member_small/"
    """卡牌资源目录名
    
    一般路径为 `cn-assets/character/member_small/resxxx_noxxx/card_xxxxx.png`
    
    图片尺寸：940x530
    """
    cards_assets_dir: str = "cards_assets/"
    """卡牌图标资源目录名"""
    card_info_file: str = "cards.json"
    """卡牌信息文件名"""
    show_trained: bool = True
    """显示特训后卡牌图片"""
    template_file: str = "gacha_10_template.html"
    """10连抽卡的HTML模板文件名"""

    @property
    def member_small_dir_path(self) -> Path:
        return get_plugin_data_file(self.member_small_dir)

    @property
    def cards_assets_dir_path(self) -> Path:
        return get_plugin_data_file(self.cards_assets_dir)

    @property
    def card_info_file_path(self) -> Path:
        return get_plugin_data_file(self.card_info_file)

    @property
    def template_dir_path(self) -> Path:
        """10连抽卡的HTML模板目录"""
        return get_plugin_config_dir()


class ScopedConfig(BaseModel):
    config_file: str = "crystal_config.json"
    """水晶配置文件路径"""
    data_file: str = "userdata.json"
    """用户数据文件路径"""
    cache_file: str = "cache.json"
    """缓存数据文件路径"""

    gacha: GachaScopedConfig = GachaScopedConfig()

    @property
    def config_file_path(self) -> Path:
        return get_plugin_config_file(self.config_file)

    @property
    def data_file_path(self) -> Path:
        return get_plugin_data_file(self.data_file)

    @property
    def cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.cache_file)


class Config(BaseModel):
    crystal: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).crystal


class CheckInConfig(BaseModel):
    """每日签到配置"""

    min_crystal: int = 10
    """每次获得的最少水晶"""
    max_crystal: int = 50
    """每次获得的最多水晶"""
    max_times: int = 3
    """每天的最大次数"""

    succeed_templates: list[str] = [
        "嗯，{daypart}。获得 {crystal} 水晶…一点点积攒起来呢。",
        "{daypart}。今天有{crystal} 水晶入账…不多，但也算个小进展。",
        "啊…{daypart}。+{crystal} 水晶。希望没弄错。",
        "嗯…{daypart}。轻轻按一下。签到成功，+{crystal} 水晶。",
        "{daypart}，签到成功。虽然不多…但每天坚持一下也挺好的。+{crystal} 水晶。",
        "{daypart}。啊…显示签到成功了。获得 {crystal} 水晶…先记着吧。",
        "{daypart}。好，签好了。今天也加 {crystal} 水晶…慢慢来。",
    ]
    """成功的消息模板列表。
    
    - `daypart`: `DailyCheckInDaypartEnum.value` 当前时间段的问候语
    - `crystal`: `int` 本次签到获得的水晶数
    """

    already_templates: list[str] = [
        "…看了一下，{daypart}的时间已经签过了。现在水晶是 {total_crystal}。",
        "嗯…{daypart}，好像之前就签到了。当前水晶：{total_crystal}。",
        "…{daypart}的时间签过了。现在水晶是 {total_crystal}。明天再继续吧。",
    ]
    """当前时间段已经签到的消息模板列表。

    - `daypart`: `DailyCheckInDaypartEnum.value` 当前时间段的问候语
    - `total_crystal`: `int` 当前水晶总数
    """

    wrong_daypart_templates: list[str] = ["嗯…现在是{daypart}的时间呢。"]
    """时间段不正确的消息模板列表。
    
    - `daypart`: `DailyCheckInDaypartEnum.value` 当前时间段的问候语
    """

    max_times_templates: list[str] = ["{daypart}。现在水晶是 {total_crystal}。明天再继续吧。"]
    """达到最大次数后的消息模板列表。
    
    - `daypart`: `DailyCheckInDaypartEnum.value` 当前时间段的问候语
    - `total_crystal`: `int` 当前水晶总数
    """

    first_use_bonus: int = 500
    """首次使用水晶系统的额外奖励水晶数"""
    first_use_templates: list[str] = [
        "欢迎你使用水晶系统。首次使用此模块，赠送{first_use_bonus}水晶。"
    ]
    """首次使用水晶系统的消息模板列表。
    
    - `first_use_bonus`: `int` 首次使用水晶系统的额外奖励水晶数
    """
    weekly_bonus: int = 300
    """每周签到满7天的额外奖励水晶数"""


class CrystalConfig(BaseModel):
    """水晶配置"""

    check_in: CheckInConfig = CheckInConfig()
    """每日签到配置"""

    handler_consumes: dict[HandlerKeyEnum, int] = {}
    """每个命令的处理函数消耗的水晶，键为唯一ID，值为消耗的水晶数"""

    handler_consume_failed_templates: list[str] = [
        "嗯…水晶好像不够。需要 {consume} 水晶…还差一些。",
        "啊…不行。当前水晶有{crystal}，需要 {consume} 水晶…有点可惜。",
        "系统提示说水晶不够…需要 {consume} 水晶。我这边也没办法跳过呢。",
        "啊…这个需要 {consume} 水晶。现在还不够，只有{crystal}…再攒几天吧。",
        "…看来用不了。需要 {consume} 水晶，现在有 {crystal}…慢慢来吧。",
        "嗯…水晶还差一点呢。需要{consume}水晶，现在只有{crystal}。抱歉，暂时用不了这个功能。",
    ]
    """命令处理函数消耗水晶失败的消息模板列表。
    
    - `consume`: `int` 该命令处理函数消耗的水晶数
    - `crystal`: `int` 当前水晶总数
    """

    gacha_probabilities: dict[RarityEnum, float] = {}
    """抽卡稀有度概率配置，键为稀有度枚举，值为对应的概率（0~1之间的浮点数）"""

    gacha_pity_probabilities: dict[RarityEnum, float] = {}
    """十连必出一张卡牌，此卡牌的稀有度概率配置"""

    gacha_bonus_crystals: dict[RarityEnum, int] = {}
    """抽卡稀有度转换为水晶的奖励配置，键为稀有度枚举，值为对应的水晶奖励数"""

    @property
    def gacha_cumulative_probabilities(self) -> dict[RarityEnum, float]:
        """抽卡稀有度的累积概率"""
        cumulative_p: dict[RarityEnum, float] = {}
        cumulative_sum = 0.0
        for r, p in self.gacha_probabilities.items():
            cumulative_sum += p
            cumulative_p[r] = cumulative_sum
        return cumulative_p

    @property
    def gacha_pity_cumulative_probabilities(self) -> dict[RarityEnum, float]:
        """十连必出一张卡牌的稀有度累积概率"""
        cumulative_p: dict[RarityEnum, float] = {}
        cumulative_sum = 0.0
        for r, p in self.gacha_pity_probabilities.items():
            cumulative_sum += p
            cumulative_p[r] = cumulative_sum
        return cumulative_p


crystal_config = load_register_model_from_file(CrystalConfig, cfg.config_file_path)


class CrystalData(BaseModel):
    console: dict[str, int] = {}
    """Console适配器用户水晶数据，键为用户ID，值为水晶数"""
    onebot: dict[str, int] = {}
    """OneBot适配器用户水晶数据，键为用户ID，值为水晶数"""

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


crystal_data = load_register_model_from_file(CrystalData, cfg.data_file_path)
