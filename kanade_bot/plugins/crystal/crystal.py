from kanade_bot.utils.common import PlatformType
from .config import crystal_data, write_crystal_data


def increment_crystal(platform: PlatformType, user_id: str, crystal: int) -> None:
    """增加用户积分"""
    data = crystal_data.get_by_platform(platform)
    data[user_id] = data.get(user_id, 0) + crystal
    write_crystal_data()


def get_crystal(platform: PlatformType, user_id: str) -> int:
    """获取用户积分"""
    data = crystal_data.get_by_platform(platform)
    return data.get(user_id, 0)


def decrement_crystal(platform: PlatformType, user_id: str, crystal: int) -> bool:
    """减少用户积分，如果积分不足则返回False"""
    data = crystal_data.get_by_platform(platform)
    current_credits = data.get(user_id, 0)
    if current_credits < crystal:
        return False

    data[user_id] = current_credits - crystal
    write_crystal_data()
    return True
