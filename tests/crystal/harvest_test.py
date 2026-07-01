import random
from pathlib import Path

from pydantic import BaseModel, field_validator


class HarvestMaterial(BaseModel):
    """采集材料配置"""

    name: str
    """材料名称"""
    bonus_crystal: int
    """材料转换为水晶的奖励数"""


class HarvestMaterialItem(BaseModel):
    """采集材料项配置"""

    material_id: str
    """材料ID"""
    quantity: int
    """材料数量"""
    probability: float
    """材料项的概率（0~1之间的浮点数）"""


class HarvestAction(BaseModel):
    """采集行为配置"""

    name: str
    """行为名称"""
    probability: float
    """行为的概率（0~1之间的浮点数）
    
    在同一行为类别下，所有行为的概率之和应为1
    """
    items: list[HarvestMaterialItem] = []
    """采集行为包含的采集材料项"""


class HarvestActionCategory(BaseModel):
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


class HarvestConfig(BaseModel):
    """采集配置"""

    materials: dict[str, HarvestMaterial] = {}
    """材料配置，键为材料ID，值为对应的材料配置"""

    action_categories: dict[str, HarvestActionCategory] = {}
    """行为配置，键为行为类别名称，值为对应的采集行为类别配置"""


p = Path("config") / "crystal" / "harvest_config.json"
harvest_config = HarvestConfig.model_validate_json(p.read_text(encoding="utf-8"))


# 蒙特卡洛模拟期望
num_simulations = 50000
total_bonus_crystal = 0
total_power_cost = 0

categories = harvest_config.action_categories
names = list(categories.keys())

for _ in range(num_simulations):
    name = random.choice(names)
    category = categories[name]

    # 从类别选择一种行为
    p = random.random()
    action_id = None
    for id, cp in category.cumulative_probabilities.items():
        if p < cp:
            action_id = id
            break
    if action_id is None:
        continue

    action = category.actions[action_id]
    # 执行采集行为
    materials: dict[str, int] = {}
    for item in action.items:
        if random.random() < item.probability:
            materials[item.material_id] = materials.get(item.material_id, 0) + item.quantity

    # 计算材料转换为水晶的奖励
    for material_id, quantity in materials.items():
        material = harvest_config.materials.get(material_id)
        if material is not None:
            total_bonus_crystal += material.bonus_crystal * quantity

    # 累加体力
    total_power_cost += category.power_cost

print(f"蒙特卡洛模拟期望的每体力水晶奖励: {total_bonus_crystal / total_power_cost}")
