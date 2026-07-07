from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel, field_validator

from kanade_bot.utils.common import AttrDocModel, PlatformType, generate_schema

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
    """抽卡环境配置"""

    config_file: str = "gacha_config.json"
    """抽卡配置文件名"""

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
    rendered_cards_dir: str = "rendered_cards/"
    """渲染后的卡牌图片缓存目录名"""

    @property
    def config_file_path(self) -> Path:
        return get_plugin_config_file(self.config_file)

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

    @property
    def rendered_cards_dir_path(self) -> Path:
        return get_plugin_cache_file(self.rendered_cards_dir)


class HarvestScopedConfig(BaseModel):
    """采集环境配置"""

    config_file: str = "harvest_config.json"
    """采集配置文件名"""

    assets_dir: str = "harvest_assets/"
    """采集物品资源目录名

    一般路径为 `cn-assets/mysekai/thumbnail/material/item_xxx_x.png`
    """

    power_cache_file: str = "harvest_power_cache.json"
    """采集体力缓存文件名"""

    result_template_file: str = "harvest_result_template.html"
    """一次采集结果HTML模板文件名"""
    results_template_file: str = "harvest_results_template.html"
    """多次采集结果HTML模板文件名"""

    crystal_image_name: str = "jewel.png"
    """水晶图片文件名"""

    daily_power: float = 10
    """每日采集体力上限"""
    power_crystal_cost: float = 10
    """每点采集体力消耗的水晶数"""

    @property
    def config_file_path(self) -> Path:
        return get_plugin_config_file(self.config_file)

    @property
    def assets_dir_path(self) -> Path:
        return get_plugin_data_file(self.assets_dir)

    @property
    def power_cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.power_cache_file)

    @property
    def template_dir_path(self) -> Path:
        """采集结果HTML模板目录"""
        return get_plugin_config_dir()

    @property
    def crystal_image_path(self) -> Path:
        return get_plugin_data_file(self.crystal_image_name)


class ScopedConfig(BaseModel):
    config_file: str = "crystal_config.json"
    """水晶配置文件名"""
    data_file: str = "userdata.json"
    """用户数据文件名"""
    cache_file: str = "cache.json"
    """缓存数据文件名"""
    weekly_check_in_file: str = "weekly_check_in.json"
    """每周签到数据文件名"""

    gacha: GachaScopedConfig = GachaScopedConfig()

    harvest: HarvestScopedConfig = HarvestScopedConfig()

    @property
    def config_file_path(self) -> Path:
        return get_plugin_config_file(self.config_file)

    @property
    def data_file_path(self) -> Path:
        return get_plugin_data_file(self.data_file)

    @property
    def cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.cache_file)

    @property
    def weekly_check_in_file_path(self) -> Path:
        return get_plugin_cache_file(self.weekly_check_in_file)


class Config(BaseModel):
    crystal: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).crystal


class CheckInConfig(AttrDocModel):
    """每日签到配置"""

    min_crystal: int = 25
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
        "{daypart}，签好了。今天也加 {crystal} 水晶…慢慢来。",
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

    weekly_bonus: int = 500
    """每周签到满7天的额外奖励水晶数"""
    weekly_bonus_templates: list[str] = ["恭喜你本周全勤签到！额外赠送{weekly_bonus}水晶。"]
    """每周签到满7天的额外奖励消息模板列表。

    - `weekly_bonus`: `int` 每周签到满7天的额外奖励水晶数
    """


class CrystalConfig(AttrDocModel):
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


generate_schema(CrystalConfig)
crystal_config = load_register_model_from_file(CrystalConfig, cfg.config_file_path)


class CrystalData(AttrDocModel):
    console: dict[str, int] = {}
    """Console适配器用户水晶数据，键为用户ID，值为水晶数"""
    onebot: dict[str, int] = {}
    """OneBot适配器用户水晶数据，键为用户ID，值为水晶数"""

    def get_by_platform(self, platform: PlatformType):
        if platform == "console":
            return self.console
        elif platform == "onebot":
            return self.onebot


generate_schema(CrystalData)
crystal_data = load_register_model_from_file(CrystalData, cfg.data_file_path)


class GachaConfig(AttrDocModel):
    """抽卡配置"""

    probabilities: dict[RarityEnum, float] = {}
    """抽卡稀有度概率配置，键为稀有度枚举，值为对应的概率（0~1之间的浮点数）"""

    pity_probabilities: dict[RarityEnum, float] = {}
    """十连必出一张卡牌，此卡牌的稀有度概率配置"""

    bonus_crystals: dict[RarityEnum, int] = {}
    """抽卡稀有度转换为水晶的奖励配置，键为稀有度枚举，值为对应的水晶奖励数"""

    @property
    def cumulative_probabilities(self) -> dict[RarityEnum, float]:
        """抽卡稀有度的累积概率"""
        cumulative_p: dict[RarityEnum, float] = {}
        cumulative_sum = 0.0
        for r, p in self.probabilities.items():
            cumulative_sum += p
            cumulative_p[r] = cumulative_sum
        return cumulative_p

    @property
    def pity_cumulative_probabilities(self) -> dict[RarityEnum, float]:
        """十连必出一张卡牌的稀有度累积概率"""
        cumulative_p: dict[RarityEnum, float] = {}
        cumulative_sum = 0.0
        for r, p in self.pity_probabilities.items():
            cumulative_sum += p
            cumulative_p[r] = cumulative_sum
        return cumulative_p

    @field_validator("probabilities")
    def validate_probabilities(cls, v: dict[RarityEnum, float]):
        """验证抽卡稀有度概率之和是否为1"""
        total_probability = sum(v.values())
        if not (0.999 <= total_probability <= 1.001):
            raise ValueError(f"抽卡稀有度概率之和应为1，当前为 {total_probability}")
        return v

    @field_validator("pity_probabilities")
    def validate_pity_probabilities(cls, v: dict[RarityEnum, float]):
        """验证十连必出一张卡牌的稀有度概率之和是否为1"""
        total_probability = sum(v.values())
        if not (0.999 <= total_probability <= 1.001):
            raise ValueError(f"十连必出一张卡牌的稀有度概率之和应为1，当前为 {total_probability}")
        return v


generate_schema(GachaConfig)
gacha_config = load_register_model_from_file(GachaConfig, cfg.gacha.config_file_path)


class HarvestMaterial(AttrDocModel):
    """采集材料配置"""

    name: str
    """材料名称"""
    bonus_crystal: int
    """材料转换为水晶的奖励数"""


class HarvestMaterialItem(AttrDocModel):
    """采集材料项配置"""

    material_id: str
    """材料ID"""
    quantity: int
    """材料数量"""
    probability: float
    """材料项的概率（0~1之间的浮点数）"""


class HarvestAction(AttrDocModel):
    """采集行为配置"""

    name: str
    """行为名称"""
    probability: float
    """行为的概率（0~1之间的浮点数）
    
    在同一行为类别下，所有行为的概率之和应为1
    """
    items: list[HarvestMaterialItem] = []
    """采集行为包含的采集材料项"""


class HarvestActionCategory(AttrDocModel):
    """采集行为类别配置"""

    power_cost: float
    """行为类别消耗的体力"""
    actions: dict[str, HarvestAction] = {}
    """行为类别包含的采集行为，键为行为ID，值为对应的采集行为配置"""

    @property
    def cumulative_probabilities(self) -> dict[str, float]:
        """采集行为类别的累积概率"""
        cumulative_p: dict[str, float] = {}
        cumulative_sum = 0.0
        for action_id, action in self.actions.items():
            cumulative_sum += action.probability
            cumulative_p[action_id] = cumulative_sum
        return cumulative_p

    @field_validator("actions")
    def validate_actions(cls, v: dict[str, HarvestAction]):
        """验证行为类别的概率之和是否为1"""
        total_probability = sum(action.probability for action in v.values())
        if not (0.999 <= total_probability <= 1.001):
            raise ValueError(f"采集行为类别的概率之和应为1，当前为 {total_probability}")
        return v


class HarvestConfig(AttrDocModel):
    """采集配置"""

    materials: dict[str, HarvestMaterial] = {}
    """材料配置，键为材料ID，值为对应的材料配置"""

    action_categories: dict[str, HarvestActionCategory] = {}
    """行为配置，键为行为类别名称，值为对应的采集行为类别配置"""


generate_schema(HarvestConfig)
harvest_config = load_register_model_from_file(HarvestConfig, cfg.harvest.config_file_path)
