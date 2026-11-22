"""网络安全日报命令冒烟测试：加载 tool 插件链路，验证抓取/缓存/图片下载

需要环境变量 SESSDATA 提供B站登录凭证。
"""

import asyncio
import os

import nonebot

nonebot.init()
nonebot.load_from_toml("pyproject.toml")

from bilibili_api import Credential

from kanade_bot.plugins.tool import security_report as sd

assert os.environ.get("SESSDATA"), "请先设置环境变量 SESSDATA"
SESSDATA = os.environ["SESSDATA"]


async def _fake_credential():
    return Credential(sessdata=SESSDATA)


# 测试环境无 parser_bili_ck/cookies，注入测试凭证
sd._get_credential = _fake_credential


async def main():
    daily = await sd.get_latest_security_daily()
    print("latest:", daily)
    assert daily is not None, "未获取到日报"

    # 验证缓存命中（第二次调用不应再发请求）
    daily2 = await sd.get_latest_security_daily()
    assert daily2 == daily, "缓存未命中"
    print("cache hit: ok")

    images = await sd.download_daily_images(daily)
    print("images:", [len(img) for img in images])
    print("cache file:", sd._daily_cache_file, sd._daily_cache_file.exists())


asyncio.run(main())
