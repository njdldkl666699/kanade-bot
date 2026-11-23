"""显卡日报命令冒烟测试：加载 tool 插件链路，验证抓取/缓存/渲染

首次运行会真实请求知乎接口与图床，需要网络可用。
"""

import asyncio
from pathlib import Path

import nonebot

# 测试环境无 config.yaml，显式启用 playwright 引擎并放宽尺寸限制（长图渲染需要）
nonebot.init(
    render={
        "provider": "playwright",
        "startup": "off",
        "provider_config": {"skip_browser_install": True, "storage_path": "~/.cache/ms-playwright"},
        "html": {"max_pixels": 2147483647, "max_auto_height": 2147483647},
    }
)
nonebot.load_from_toml("pyproject.toml")

from kanade_bot.plugins.tool import gpu_daily


async def main():
    report = await gpu_daily.get_latest_gpu_daily()
    assert report is not None, "未获取到显卡日报"
    print("latest:", report.article_id, report.title, report.created, "fresh:", report.is_fresh())

    # 验证缓存命中（第二次调用不应再发请求）
    report2 = await gpu_daily.get_latest_gpu_daily()
    assert report2 == report, "缓存未命中"
    print("cache hit: ok")

    # 验证缓存持久化可回读
    gpu_daily._cache.clear()
    gpu_daily._load_cache()
    assert gpu_daily._newest_cached() == report, "缓存持久化回读失败"
    print("persist round-trip: ok")

    image = await gpu_daily.render_gpu_daily(report)
    assert image[:3] == b"\xff\xd8\xff", "渲染结果不是 JPEG"
    print("rendered jpeg bytes:", len(image))
    out = "/tmp/gpu_daily_render.jpg"
    await asyncio.to_thread(Path(out).write_bytes, image)
    print("saved to:", out)


asyncio.run(main())
