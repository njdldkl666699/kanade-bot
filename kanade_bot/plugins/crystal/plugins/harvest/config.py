# ruff: noqa: RUF012
from pathlib import Path

from nonebot import get_plugin_config, require
from pydantic import BaseModel, field_validator

from kanade_bot.utils.common import AttrDocModel, generate_schema

require("nonebot_plugin_localstore")
require("model_updater")

from nonebot_plugin_localstore import (
    get_plugin_cache_file,
    get_plugin_config_dir,
    get_plugin_config_file,
    get_plugin_data_file,
)

from kanade_bot.plugins.model_updater import load_register_model_from_file


class ScopedConfig(BaseModel):
    """采集环境配置"""

    config_file: str = "harvest_config.json"
    """采集配置文件名"""

    assets_dir: str = "harvest_assets/"
    """采集物品资源目录名

    一般路径为 `cn-assets/mysekai/thumbnail/material/item_xxx_x.png`
    """

    power_cache_file: str = "harvest_power_cache.json"
    """采集体力缓存文件名"""
    crystal_power_cache_file: str = "harvest_crystal_power_cache.json"
    """水晶恢复的体力数据文件名（不会被每日清除）"""

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
    def crystal_power_cache_file_path(self) -> Path:
        return get_plugin_cache_file(self.crystal_power_cache_file)

    @property
    def template_dir_path(self) -> Path:
        """采集结果HTML模板目录"""
        return get_plugin_config_dir()

    @property
    def crystal_image_path(self) -> Path:
        return get_plugin_data_file(self.crystal_image_name)


class Config(BaseModel):
    harvest: ScopedConfig = ScopedConfig()


cfg = get_plugin_config(Config).harvest


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
harvest_config = load_register_model_from_file(HarvestConfig, cfg.config_file_path)
