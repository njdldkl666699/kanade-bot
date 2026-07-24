# ruff: noqa: RUF012
from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel, field_validator

from kanade_bot.utils.common import AttrDocModel, generate_schema

from .enum import RarityEnum

require("nonebot_plugin_localstore")
require("model_updater")

from nonebot_plugin_localstore import (
    get_plugin_cache_dir,
    get_plugin_config_file,
    get_plugin_data_file,
)

from kanade_bot.plugins.model_updater import load_register_model_from_file


class ScopedConfig(BaseModel):
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
    def rendered_cards_dir_path(self) -> Path:
        return get_plugin_cache_dir()


class Config(BaseModel):
    gacha: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).gacha


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
gacha_config = load_register_model_from_file(GachaConfig, cfg.config_file_path)
