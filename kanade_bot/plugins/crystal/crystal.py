import random

from nonebot.matcher import Matcher

from kanade_bot.utils.common import PlatformType

from .config import HandlerKeyEnum, crystal_config, crystal_data


def increment_crystal(platform: PlatformType, user_id: str, crystal: int) -> None:
    """增加用户积分"""
    data = crystal_data.instance.get_by_platform(platform)
    data[user_id] = data.get(user_id, 0) + crystal
    crystal_data.save_to_file()


def get_crystal(platform: PlatformType, user_id: str) -> int:
    """获取用户积分"""
    data = crystal_data.instance.get_by_platform(platform)
    return data.get(user_id, 0)


def check_user_crystal(
    handler_key: HandlerKeyEnum,
    platform: PlatformType,
    user_id: str,
) -> bool:
    """检查用户水晶是否足够"""
    consume = crystal_config.instance.handler_consumes.get(handler_key, 0)
    if consume <= 0:
        return True
    return get_crystal(platform, user_id) >= consume


def succeed_consume(
    handler_key: HandlerKeyEnum,
    platform: PlatformType,
    user_id: str,
):
    """处理水晶消耗成功的情况，并扣除水晶"""
    consume = crystal_config.instance.handler_consumes.get(handler_key, 0)
    if consume <= 0:
        return True

    data = crystal_data.instance.get_by_platform(platform)
    current_crystal = data.get(user_id, 0)
    if current_crystal < consume:
        raise ValueError("用户水晶不足，无法扣除")

    data[user_id] = current_crystal - consume
    crystal_data.save_to_file()


async def finish_fail_consume(
    matcher: type[Matcher],
    handler_key: HandlerKeyEnum,
    platform: PlatformType,
    user_id: str,
):
    """处理水晶不足的情况，发送提示消息并结束事件处理"""
    consume = crystal_config.instance.handler_consumes.get(handler_key, 0)
    user_crystal = get_crystal(platform, user_id)
    template = random.choice(crystal_config.instance.handler_consume_failed_templates)
    message = template.format(consume=consume, crystal=user_crystal)
    await matcher.finish(message)
