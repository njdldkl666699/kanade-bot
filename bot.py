import os

import nonebot
from dotenv import load_dotenv
from nonebot.adapters.console import Adapter as ConsoleAdapter
from nonebot.adapters.onebot.v11 import Adapter as OneBotV11Adapter

from scripts.banner import get_kanade

load_dotenv()

CHAT_ENABLED = os.getenv("CHAT_ENABLED", "true").lower() == "true"


def init_nonebot():
    # 初始化 NoneBot
    nonebot.init()

    # 注册适配器
    driver = nonebot.get_driver()
    driver.register_adapter(ConsoleAdapter)
    driver.register_adapter(OneBotV11Adapter)

    # 在这里加载插件
    nonebot.load_builtin_plugins("echo")

    nonebot.load_plugin("nonebot_plugin_status")
    nonebot.load_plugin("nonebot_plugin_apscheduler")
    nonebot.load_plugin("nonebot_plugin_htmlrender")

    # nonebot.load_plugins("kanade_bot/plugins")
    nonebot.load_plugin("kanade_bot.plugins.api60s")
    nonebot.load_plugin("kanade_bot.plugins.fun")
    nonebot.load_plugin("kanade_bot.plugins.help")
    nonebot.load_plugin("kanade_bot.plugins.summary")
    nonebot.load_plugin("kanade_bot.plugins.tool")

    if CHAT_ENABLED:
        nonebot.load_plugin("kanade_bot.plugins.chat")


if __name__ == "__main__":
    print(get_kanade())
    init_nonebot()
    nonebot.run()
