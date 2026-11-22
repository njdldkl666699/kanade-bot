"""网络安全周报命令冒烟测试：加载 tool 插件链路，验证抓取/缓存/图片下载

需要环境变量 SESSDATA 提供B站登录凭证。
"""

import asyncio
import os

import nonebot

nonebot.init()
nonebot.load_from_toml("pyproject.toml")

from bilibili_api import Credential

from kanade_bot.plugins.tool import security_report as sw

assert os.environ.get("SESSDATA"), "请先设置环境变量 SESSDATA"
SESSDATA = os.environ["SESSDATA"]


async def _fake_credential():
    return Credential(sessdata=SESSDATA)


# 测试环境无 parser_bili_ck/cookies，注入测试凭证
sw._get_credential = _fake_credential


async def main():
    weekly = await sw.get_latest_security_weekly()
    print("latest:", weekly)
    assert weekly is not None, "未获取到周报"
    assert (weekly.end_date - weekly.start_date).days == 6, "周报区间应为 7 天"
    # 2026-11-22 时最新一期应对应示例链接的动态，之后新发布一期则断言失败（属预期，更新期望值即可）
    assert weekly.dynamic_url == "https://www.bilibili.com/opus/1272726572248334345", (
        weekly.dynamic_url
    )

    # 验证缓存命中（第二次调用不应再发请求）
    weekly2 = await sw.get_latest_security_weekly()
    assert weekly2 == weekly, "缓存未命中"
    print("cache hit: ok")

    images = await sw.download_weekly_images(weekly)
    print("images:", [len(img) for img in images])
    print("cache file:", sw._weekly_cache_file, sw._weekly_cache_file.exists())


asyncio.run(main())
