"""显卡日报命令冒烟测试：加载 tool 插件链路，验证抓取/缓存/回看/渲染

首次运行会真实请求知乎接口与图床，需要网络可用。
"""

import asyncio
import json
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
from kanade_bot.utils.common import asia_shanghai_now


async def main():
    reports = await gpu_daily.get_gpu_daily_reports()
    assert reports, "未获取到显卡日报"
    latest = reports[0]
    print("latest:", latest.article_id, latest.title, latest.created)
    assert len(reports) >= 2, "应至少缓存两期（中午加更与前一晚日常）"
    print("previous:", reports[1].article_id, reports[1].title, reports[1].created)

    # 验证缓存命中（第二次调用不应再发请求）
    reports2 = await gpu_daily.get_gpu_daily_reports()
    assert reports2[0] == latest, "缓存未命中"
    print("cache hit: ok, cached issues:", len(reports2))

    # 验证旧格式（纯 {id: report} 字典）可迁移回读
    old_format = json.dumps(
        {r.article_id: r.model_dump(mode="json") for r in reports}, ensure_ascii=False
    )
    await asyncio.to_thread(gpu_daily._cache_file.write_text, old_format, "utf-8")
    gpu_daily._cache.clear()
    gpu_daily._last_check = None
    gpu_daily._load_cache()
    assert gpu_daily._sorted_reports()[0] == latest, "旧格式迁移回读失败"
    assert gpu_daily._last_check is None, "旧格式不应有 last_check"
    print("legacy migration: ok")

    # 验证新格式持久化回读（含 last_check）
    gpu_daily._last_check = asia_shanghai_now()
    gpu_daily._save_cache()
    gpu_daily._cache.clear()
    gpu_daily._last_check = None
    gpu_daily._load_cache()
    assert gpu_daily._sorted_reports()[0] == latest, "缓存持久化回读失败"
    assert gpu_daily._last_check is not None, "last_check 未持久化"
    print("persist round-trip: ok, last_check:", gpu_daily._last_check)

    # 渲染上一期（前一晚的日常日报）验证回看路径
    image = await gpu_daily.render_gpu_daily(reports[1])
    assert image[:3] == b"\xff\xd8\xff", "渲染结果不是 JPEG"
    print("rendered jpeg bytes:", len(image))
    out = "/tmp/gpu_daily_prev.jpg"
    await asyncio.to_thread(Path(out).write_bytes, image)
    print("saved to:", out)


asyncio.run(main())
