import os
import re
from pathlib import Path
from typing import Any

import nonebot
from nonebot import logger
from nonebot.adapters.console import Adapter as ConsoleAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter
from nonebot.compat import model_dump
from nonebot.config import DOTENV_TYPE, Config, Env
from nonebot.utils import escape_tag

from kanade_bot.utils.banner import get_kanade
from kanade_bot.utils.onebot11 import BotOfflineNoticeEvent
from scripts.util import load_configs


def init_nonebot(
    *,
    env: Env = Env(),
    _env_file: DOTENV_TYPE | None = None,
    **kwargs: Any,
):
    """初始化 NoneBot 以及 全局 {ref}`nonebot.drivers.Driver` 对象。

    NoneBot 将会从 .env 文件中读取环境信息，并使用相应的 env 文件配置。

    也可以传入自定义的 `_env_file` 来指定 NoneBot 从该文件读取配置。

    Args:
        _env_file: 配置文件名，默认从 `.env.{env_name}` 中读取配置
        kwargs: 任意变量，将会存储到 {ref}`nonebot.drivers.Driver.config` 对象里

    Examples:
        ```python
        init_nonebot(database=Database(...))
        ```
    """
    logger.success("NoneBot is initializing...")

    _env_file = _env_file or f".env.{env.environment}"
    config = Config(
        **kwargs,
        _env_file=((".env", _env_file) if isinstance(_env_file, (str, os.PathLike)) else _env_file),
    )

    logger.configure(extra={"nonebot_log_level": config.log_level}, patcher=nonebot._log_patcher)
    logger.opt(colors=True).info(f"Current <y><b>Env: {escape_tag(env.environment)}</b></y>")
    logger.opt(colors=True).debug(
        f"Loaded <y><b>Config</b></y>: {escape_tag(str(model_dump(config)))}"
    )

    DriverClass = nonebot._resolve_combine_expr(config.driver)
    nonebot._driver = DriverClass(env, config)


def register_adapters_and_load_plugins():
    """注册适配器和加载插件"""
    # 注册适配器
    driver = nonebot.get_driver()
    driver.register_adapter(ConsoleAdapter)
    driver.register_adapter(OneBotV11Adapter)

    # 为 OneBotV11Adapter 添加自定义事件模型
    nonebot.get_adapter(OneBotV11Adapter).add_custom_model(BotOfflineNoticeEvent)

    # 在这里加载插件
    nonebot.load_from_toml("pyproject.toml")


def patch_foreign_plugins():
    """对第三方插件进行必要的修补"""
    ## echo
    from nonebot.plugins.echo import echo
    from nonebot.rule import ToMeRule

    # 阻止 echo 的指令向后传播
    echo.block = True
    # 移除to_me()规则
    echo.rule.checkers = {
        checker for checker in echo.rule.checkers if checker.call.__class__ is not ToMeRule
    }

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

    ## nonebot_plugin_picstatus
    from nonebot_plugin_picstatus.__main__ import stat_matcher

    # 阻止 PicStatus 的指令向后传播
    stat_matcher.block = True
    stat_matcher.priority = 2


def register_other_configs_and_generate_schema():
    """注册适配器和第三方插件的配置类，并生成合并后的配置Schema"""
    from nonebot.adapters.console.config import Config as ConsoleAdapterConfig
    from nonebot.adapters.onebot.v11.config import Config as OneBotV11AdapterConfig

    from kanade_bot.utils.schema import ConfigRegistry

    ConfigRegistry.register_config_types(ConsoleAdapterConfig, OneBotV11AdapterConfig)

    from nonebot_plugin_alconna.config import Config as AlconnaConfig
    from nonebot_plugin_apscheduler.config import Config as APSchedulerConfig
    from nonebot_plugin_chatrecorder.config import Config as ChatRecorderConfig

    # from nonebot_plugin_datastore.config import Config as DataStoreConfig
    from nonebot_plugin_htmlrender.config import Config as HTMLRenderConfig
    from nonebot_plugin_localstore.config import Config as LocalStoreConfig

    # from nonebot_plugin_orm.config import Config as ORMConfig
    from nonebot_plugin_permission.config import Config as PermissionConfig
    from nonebot_plugin_picstatus.config import ConfigModel as PicStatusConfig
    from nonebot_plugin_uninfo.config import Config as UninfoConfig
    from nonebot_plugin_user.config import Config as UserConfig
    from nonebot_plugin_waiter.config import Config as WaiterConfig
    from nonebot_plugin_whateat_pic.config import Config as WhateatPicConfig
    # from nonebot_plugin_wordcloud.config import Config as WordCloudConfig

    ConfigRegistry.register_config_types(
        ConsoleAdapterConfig,
        OneBotV11AdapterConfig,
        AlconnaConfig,
        APSchedulerConfig,
        ChatRecorderConfig,
        # DataStoreConfig,
        HTMLRenderConfig,
        LocalStoreConfig,
        # ORMConfig,
        PermissionConfig,
        PicStatusConfig,
        UninfoConfig,
        UserConfig,
        WaiterConfig,
        WhateatPicConfig,
        # WordCloudConfig,
    )

    ConfigRegistry.generate_merged_config_schema()


if __name__ == "__main__":
    print(get_kanade())
    env, configs = load_configs(Path(__file__).parent)
    init_nonebot(env=env, **configs)
    register_adapters_and_load_plugins()
    patch_foreign_plugins()
    register_other_configs_and_generate_schema()
    nonebot.run()
