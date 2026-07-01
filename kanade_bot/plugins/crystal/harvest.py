import base64
import random
from dataclasses import dataclass
from pathlib import Path

from nonebot import require

from kanade_bot.utils.common import PlatformType

from .cache import get_or_init_harvest_power, harvest_power_cache
from .config import cfg as scoped_cfg
from .config import harvest_config
from .crystal import increment_crystal

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import template_to_pic

cfg = scoped_cfg.harvest


@dataclass
class HarvestResult:
    """采集结果"""

    category_name: str
    """行为类别名"""
    power_cost: float
    """消耗体力"""
    action_name: str
    """行为名称"""
    materials: dict[str, int]
    """材料及数量"""
    bonus_crystal: int
    """材料转换为水晶的奖励数"""


def harvest_once(
    platform: PlatformType,
    user_id: str,
    category_name: str | None = None,
) -> HarvestResult | None:
    """执行一次采集资源操作

    Args:
        platform: 平台类型
        user_id: 用户ID
        category: 采集资源类别，为空随机选择。

    Returns:
        采集结果，如果体力不足则返回 None。
    """
    name = category_name
    # 检查剩余体力
    power = get_or_init_harvest_power(platform, user_id)

    v = harvest_config.instance
    categories = v.action_categories
    # 检查采集类别
    if name is None:
        # 随机从剩余体力支持的类别中选择
        available_names: list[str] = []
        for n, c in categories.items():
            if c.power_cost <= power:
                available_names.append(n)
            elif abs(c.power_cost - power) < 1e-6:
                # 考虑浮点数误差，允许体力与消耗相等的情况
                power = c.power_cost
                available_names.append(n)

        if not available_names:
            return None
        name = random.choice(available_names)

    if name not in categories:
        return None
    category = categories[name]
    if category.power_cost > power:
        return None

    # 从类别选择一种行为
    p = random.random()
    action_id = None
    for id, cp in category.cumulative_probabilities.items():
        if p < cp:
            action_id = id
            break
    if action_id is None:
        return None

    action = category.actions[action_id]
    # 执行采集行为
    materials: dict[str, int] = {}
    for item in action.items:
        if random.random() < item.probability:
            materials[item.material_id] = materials.get(item.material_id, 0) + item.quantity

    # 计算材料转换为水晶的奖励
    bonus_crystal = 0
    for material_id, quantity in materials.items():
        material = v.materials.get(material_id)
        if material is not None:
            bonus_crystal += material.bonus_crystal * quantity

    # 增加水晶
    increment_crystal(platform, user_id, bonus_crystal)
    # 扣除体力
    harvest_power_cache.set(platform, user_id, power - category.power_cost)

    # 返回采集结果
    return HarvestResult(
        category_name=name,
        power_cost=category.power_cost,
        action_name=action.name,
        materials=materials,
        bonus_crystal=bonus_crystal,
    )


def _image_to_data_url(path: Path) -> str:
    if not path.is_file():
        return ""

    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _material_template_data(
    materials: dict[str, int],
) -> tuple[dict[str, str], dict[str, str]]:
    v = harvest_config.instance
    material_images: dict[str, str] = {}
    material_names: dict[str, str] = {}

    for material_id in materials:
        material = v.materials.get(material_id)
        material_names[material_id] = material.name if material is not None else material_id

        image_path = cfg.assets_dir_path / f"{material_id}.png"
        material_images[material_id] = _image_to_data_url(image_path)

    return material_names, material_images


async def render_harvest_result(result: HarvestResult) -> bytes:
    material_names, material_images = _material_template_data(result.materials)

    return await template_to_pic(
        template_path=str(cfg.template_dir_path),
        template_name=cfg.result_template_file,
        templates={
            "category_name": result.category_name,
            "power_cost": f"{result.power_cost:g}",
            "action_name": result.action_name,
            "materials": result.materials,
            "material_names": material_names,
            "material_images": material_images,
            "bonus_crystal": result.bonus_crystal,
            "crystal_image": _image_to_data_url(cfg.crystal_image_path),
        },
    )


async def render_harvest_results(results: list[HarvestResult]) -> bytes:
    materials: dict[str, int] = {}
    for result in results:
        for material_id, quantity in result.materials.items():
            materials[material_id] = materials.get(material_id, 0) + quantity

    material_names, material_images = _material_template_data(materials)
    result_items = [
        {
            "category_name": result.category_name,
            "power_cost": f"{result.power_cost:g}",
            "action_name": result.action_name,
            "materials": result.materials,
        }
        for result in results
    ]

    return await template_to_pic(
        template_path=str(cfg.template_dir_path),
        template_name=cfg.results_template_file,
        templates={
            "results": result_items,
            "result_count": len(results),
            "total_power_cost": f"{sum(result.power_cost for result in results):g}",
            "materials": materials,
            "material_names": material_names,
            "material_images": material_images,
            "bonus_crystal": sum(result.bonus_crystal for result in results),
            "crystal_image": _image_to_data_url(cfg.crystal_image_path),
        },
    )
