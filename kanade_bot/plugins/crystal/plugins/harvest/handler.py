from nonebot.adapters import Event, Message
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg

from kanade_bot.plugins.crystal.config import crystal_data
from kanade_bot.utils.common import get_platform_type

from .cache import harvest_power_cache
from .config import cfg, harvest_config
from .harvest import HarvestResult, harvest_once, render_harvest_result, render_harvest_results
from .matcher import harvest, harvest_all, harvest_category, harvest_power, resume_harvest_power


@harvest.handle()
async def _(event: Event, arg_msg: Message = CommandArg()):
    user_id = event.get_user_id()

    category_name = arg_msg.extract_plain_text().strip()
    if not category_name:
        category_name = None

    result = harvest_once(get_platform_type(event), event.get_user_id(), category_name)
    if result is None:
        await harvest.finish("采集失败，体力不足或类别不存在。")

    if isinstance(event, ConsoleEvent):
        texts = [
            f"{result.category_name}-{result.action_name} 采集成功！",
            f"消耗体力: {result.power_cost}",
            f"采集到的材料: {result.materials}",
            f"额外水晶奖励: {result.bonus_crystal}",
        ]
        await harvest.finish("\n".join(texts))
    elif isinstance(event, OneBotEvent):
        image = await render_harvest_result(result)
        message = OneBotMessage()
        message += OneBotMessageSegment.at(user_id)
        message += OneBotMessageSegment.image(image)
        await harvest.finish(message)


@harvest_all.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()
    results: list[HarvestResult] = []

    while True:
        result = harvest_once(platform, user_id)
        if result is None:
            break
        results.append(result)

    if not results:
        await harvest_all.finish("采集失败，体力不足。")

    if isinstance(event, ConsoleEvent):
        total_power_cost = sum(result.power_cost for result in results)
        total_bonus_crystal = sum(result.bonus_crystal for result in results)
        texts = [
            f"采集全部资源完成，共采集 {len(results)} 次。",
            f"消耗体力: {total_power_cost:g}",
            f"额外水晶奖励: {total_bonus_crystal}",
        ]
        await harvest_all.finish("\n".join(texts))
    elif isinstance(event, OneBotEvent):
        image = await render_harvest_results(results)
        message = OneBotMessage()
        message += OneBotMessageSegment.at(user_id)
        message += OneBotMessageSegment.image(image)
        await harvest_all.finish(message)


@harvest_category.handle()
async def _():
    texts = ["可用的采集资源类别、消耗体力："]
    for name, category in harvest_config.instance.action_categories.items():
        texts.append(f"{name}: {category.power_cost} 体力")
    await harvest_category.finish("\n".join(texts))


@harvest_power.handle()
async def _(event: Event):
    platform = get_platform_type(event)
    user_id = event.get_user_id()
    current_power = harvest_power_cache.get(platform, user_id)
    await harvest_power.finish(f"你当前的体力值为: {current_power:g}。")


@resume_harvest_power.handle()
async def _(event: Event, arg_msg: Message = CommandArg()):
    power = arg_msg.extract_plain_text().strip()
    try:
        power = float(power)
    except ValueError:
        await resume_harvest_power.finish("请输入有效的体力值。")
    if power <= 0:
        await resume_harvest_power.finish("体力值必须大于 0。")

    platform = get_platform_type(event)
    user_id = event.get_user_id()

    crystal_cost = int(power * cfg.power_crystal_cost)
    data = crystal_data.instance.get_by_platform(platform)
    user_crystal = data.get(user_id, 0)
    if user_crystal < crystal_cost:
        await resume_harvest_power.finish(
            f"水晶不足，恢复 {power} 体力需要 {crystal_cost} 水晶，你当前有 {user_crystal} 水晶。"
        )

    # 增加体力
    current_power = harvest_power_cache.get(platform, user_id)
    new_power = current_power + power
    harvest_power_cache.set_by(platform, user_id, power)
    # 扣除水晶
    data[user_id] = user_crystal - crystal_cost
    crystal_data.save_to_file()

    await resume_harvest_power.finish(
        f"已恢复 {power} 体力，消耗 {crystal_cost} 水晶，你当前体力为 {new_power:g}。"
    )
