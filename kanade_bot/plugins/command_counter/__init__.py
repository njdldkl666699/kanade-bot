from datetime import date

from nonebot import logger
from nonebot.matcher import Matcher
from nonebot.plugin import PluginMetadata

from . import handler as _  # noqa: F401
from .config import Config, command_counter_data

__plugin_meta__ = PluginMetadata(
    name="command_counter",
    description="统计命令调用次数",
    usage="",
    config=Config,
)


def register_matcher(matcher: type[Matcher], name: str):
    """注册一个 Matcher，开始计数该 Matcher 的调用次数

    注意：该函数应在 Matcher 注册后立即调用，本函数会在 Matcher 的
    handlers 追加一个 handler 用于计数调用次数。
    """
    logger.debug(f"命令计数器注册 Matcher {name}: {repr(matcher)}")

    @matcher.handle()
    def _():
        today = date.today()
        data = command_counter_data.root
        if today not in data:
            data[today] = {}
        data[today][name] = data[today].get(name, 0) + 1


__all__ = ["register_matcher"]
