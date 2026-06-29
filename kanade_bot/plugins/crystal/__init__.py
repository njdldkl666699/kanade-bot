from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config
from .crystal import check_user_crystal, finish_fail_consume, succeed_consume
from .enum import HandlerKeyEnum

__plugin_meta__ = PluginMetadata(
    name="crystal",
    description="水晶（积分）系统",
    usage="",
    config=Config,
)


__all__ = [
    "HandlerKeyEnum",
    "check_user_crystal",
    "succeed_consume",
    "finish_fail_consume",
]
