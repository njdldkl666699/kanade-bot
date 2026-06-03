import re

import nonebot
from nonebot.adapters.console import Adapter as ConsoleAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from kanade_bot.utils.banner import get_kanade
from kanade_bot.utils.onebot11 import BotOfflineNoticeEvent


def init_nonebot():
    # 初始化 NoneBot
    nonebot.init()

    # 注册适配器
    driver = nonebot.get_driver()
    driver.register_adapter(ConsoleAdapter)
    driver.register_adapter(OneBotV11Adapter)

    # 为 OneBotV11Adapter 添加自定义事件模型
    nonebot.get_adapter(OneBotV11Adapter).add_custom_model(BotOfflineNoticeEvent)

    # 在这里加载插件
    nonebot.load_builtin_plugins("echo")

    nonebot.load_plugin("nonebot_plugin_apscheduler")
    nonebot.load_plugin("nonebot_plugin_htmlrender")
    nonebot.load_plugin("nonebot_plugin_localstore")
    nonebot.load_plugin("nonebot_plugin_picstatus_ng")
    nonebot.load_plugin("nonebot_plugin_whateat_pic")

    nonebot.load_plugins("kanade_bot/plugins")


def patch_foreign_plugins():
    ## nonebot_plugin_whateat_pic
    from nonebot_plugin_whateat_pic.matcher import (
        add_menu_matcher,
        del_menu_matcher,
        drink_pic_matcher,
        eat_pic_matcher,
        view_menu_matcher,
    )

    # 删除原有的错误快捷方式
    eat_pic_matcher.shortcut(
        r"[今|明|后]?[天|日]?(早|中|晚)?(上|午|餐|饭|夜宵|宵夜|早|晚)吃(什么|啥|点啥)",
        delete=True,
    )  # pyright: ignore[reportCallIssue]
    drink_pic_matcher.shortcut(
        r"[今|明|后]?[天|日]?(早|中|晚)?(上|午|餐|饭|夜宵|宵夜|早|晚)喝(什么|啥|点啥)",
        delete=True,
    )  # pyright: ignore[reportCallIssue]

    # 添加新的快捷方式
    pattern = r"""
        (?:                             # 时间词（可选）
            [今明后]                     # 今/明/后
            [天日]?                      # 天/日（可选）
        )?
        (?:                             # 餐段词（可选）
            (?:早|午|晚)(?:餐|饭)?       # 早餐、午餐、晚餐、早饭等
            |早上|中午|晚上              # 完整时间词
            |宵夜|夜宵                   # 宵夜相关
            |(?<=[今明])晚               # 今晚、明晚
        )?
        {action}(?:什么|啥|点啥)         # 核心动词（必须）
    """

    eat_pattern = pattern.format(action="吃")
    drink_pattern = pattern.format(action="喝")

    eat_regex = re.compile(eat_pattern, re.VERBOSE)
    drink_regex = re.compile(drink_pattern, re.VERBOSE)

    eat_pic_matcher.shortcut(eat_regex, fuzzy=False, prefix=True)
    drink_pic_matcher.shortcut(drink_regex, fuzzy=False, prefix=True)

    # 阻止nonebot_plugin_whateat_pic的指令向后传播
    eat_pic_matcher.block = True
    drink_pic_matcher.block = True
    view_menu_matcher.block = True
    add_menu_matcher.block = True
    del_menu_matcher.block = True
    # 降低优先级
    eat_pic_matcher.priority = 2
    drink_pic_matcher.priority = 2
    view_menu_matcher.priority = 2
    add_menu_matcher.priority = 2
    del_menu_matcher.priority = 2

    ## nonebot_plugin_picstatus_ng
    from nonebot_plugin_picstatus_ng.__main__ import stat_matcher

    # 阻止 PicStatus 的指令向后传播
    stat_matcher.block = True
    stat_matcher.priority = 2


if __name__ == "__main__":
    print(get_kanade())
    init_nonebot()
    patch_foreign_plugins()
    nonebot.run()
