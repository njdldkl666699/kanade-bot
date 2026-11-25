"""橘鸦AI早报命令冒烟测试：加载 tool 插件链路，验证解析/抓取/缓存/整期渲染

首次运行会真实请求 daily.juya.uk 与 assets.juya.uk，需要网络可用。
"""

import asyncio
from datetime import date
from pathlib import Path
from unittest.mock import patch

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

from kanade_bot.plugins.tool import juya_daily


async def main():
    # 参数解析
    assert juya_daily.parse_days_ago("") == 0
    assert juya_daily.parse_days_ago("0") == 0
    assert juya_daily.parse_days_ago("今天") == 0
    assert juya_daily.parse_days_ago("1") == 1
    assert juya_daily.parse_days_ago("昨天") == 1
    assert juya_daily.parse_days_ago("3") == 3
    assert juya_daily.parse_days_ago("3天") == 3
    assert juya_daily.parse_days_ago("3天前") == 3
    assert juya_daily.parse_days_ago("9999天") == 9999
    assert juya_daily.parse_days_ago("abc") is None
    assert juya_daily.parse_days_ago("-1") is None
    assert juya_daily.parse_days_ago("1.5") is None
    print("parse_days_ago: ok")

    # 期数解析与回退
    issues = [date(2026, 11, 24), date(2026, 11, 23), date(2026, 11, 22)]
    assert juya_daily.resolve_issue_date(issues, date(2026, 11, 25)) == date(2026, 11, 24)
    assert juya_daily.resolve_issue_date(issues, date(2026, 11, 24)) == date(2026, 11, 24)
    assert juya_daily.resolve_issue_date(issues, date(2026, 11, 23)) == date(2026, 11, 23)
    assert juya_daily.resolve_issue_date(issues, date(2026, 11, 21)) is None
    print("resolve_issue_date: ok")

    # 硬换行合并：CJK 断点不补空格，字母数字断点补空格，引用保留前缀
    # （引用续行无 > 前缀，为 CommonMark 的懒惰延续行，应并入引用）
    reflowed = juya_daily._reflow_markdown(
        "智谱发布GLM\n5.4模型\n\n> 官方称\n模型对齐\n\n普通段落\n"
    )
    assert reflowed == "智谱发布GLM 5.4模型\n\n> 官方称模型对齐\n\n普通段落", reflowed
    # 引用紧邻段落与整行图片的边界
    reflowed = juya_daily._reflow_markdown(
        "标题后段落第\n一行\n> 引述内\n容\n![](https://example.com/a.png)\n"
    )
    assert reflowed == "标题后段落第一行\n> 引述内容\n![](https://example.com/a.png)", reflowed
    print("reflow: ok")

    # 概览提取与行内标记清理
    sample = (
        "# AI 早报 2026-11-24\n"
        "\n"
        "**视频版**：[哔哩哔哩](https://www.bilibili.com/video/BV1NRyRzcEZL) ｜ [YouTube](https://x)\n"
        "\n"
        "## 概览\n"
        "### 要闻\n"
        "- **智谱**发布 `GLM-5.4` [↗](https://a) `#1`\n"
        "### 行业动态\n"
        "- 无编号条目 [↗](https://b)\n"
        "\n"
        "---\n"
        "\n"
        "## 要闻"
    )
    assert juya_daily.extract_video_url(sample) == "https://www.bilibili.com/video/BV1NRyRzcEZL"
    overview = juya_daily.overview_text(sample)
    assert overview == "【要闻】\n1. 智谱发布 GLM-5.4\n\n【行业动态】\n- 无编号条目", overview
    assert juya_daily.extract_video_url("无视频行") is None
    assert juya_daily.overview_text("## 概览\n\n---") == ""
    print("overview/video: ok")

    # 真实请求：期数列表
    issues = await juya_daily.get_juya_issues()
    assert issues, "未获取到期数列表"
    assert issues == sorted(issues, reverse=True)
    latest = issues[0]
    print("issues:", len(issues), "latest:", latest)

    # 验证缓存命中（第二次调用不应再发请求）
    assert await juya_daily.get_juya_issues() == issues
    print("issues cache hit: ok")

    # 真实请求：最新一期正文
    markdown = await juya_daily.get_juya_daily_markdown(latest)
    assert markdown, "未获取到早报正文"
    assert "# AI 早报" in markdown
    print(
        "markdown:",
        len(markdown),
        "chars; overview lines:",
        juya_daily.overview_text(markdown).count("\n") + 1,
    )

    # 不存在的日期（首期之前）应返回 None
    assert await juya_daily.get_juya_daily_markdown(date(2026, 1, 1)) is None
    print("404 -> None: ok")

    # 缓存持久化回读
    juya_daily._markdowns.clear()
    juya_daily._issues.clear()
    juya_daily._issues_last_check = None
    juya_daily._load_cache()
    assert latest in juya_daily._markdowns, "缓存持久化回读失败"
    assert juya_daily._issues and juya_daily._issues[0] == latest, "期数列表回读失败"
    assert juya_daily._issues_last_check is not None
    print("persist round-trip: ok")

    # 默认仅渲染文字：配图行被移除，不访问网络
    assert not juya_daily.render_images_enabled()
    prepared = await juya_daily._prepare_markdown(
        "# 标题\n\n![](https://example.com/a.png)\n\n正文\n"
    )
    assert "![" not in prepared, "文字模式未移除配图行"
    assert "data:" not in prepared
    assert "正文" in prepared
    print("text prepare: ok")

    # 整期渲染（仅文字，无需下载配图）与渲染缓存
    text_image = await juya_daily.render_juya_daily(latest, markdown)
    assert text_image[:3] == b"\xff\xd8\xff", "渲染结果不是 JPEG"
    print("text rendered jpeg bytes:", len(text_image))

    text_cache = juya_daily._render_cache_path(latest, markdown)
    assert text_cache.is_file(), "渲染图未写入缓存"
    assert await juya_daily.render_juya_daily(latest, markdown) == text_image, "渲染缓存未命中"
    print("text render cache hit: ok")

    # 开启配图渲染：缓存键区分模式，整期渲染需下载全部配图
    with patch.object(juya_daily.cfg, "juya_daily_render_images", True):
        assert juya_daily.render_images_enabled()
        img_cache = juya_daily._render_cache_path(latest, markdown)
        assert img_cache != text_cache
        image = await juya_daily.render_juya_daily(latest, markdown)
        assert image[:3] == b"\xff\xd8\xff", "配图模式渲染结果不是 JPEG"
        assert img_cache.is_file(), "配图模式渲染图未写入缓存"
        print("image rendered jpeg bytes:", len(image))

    # 正文变更后渲染缓存应失效（新文件名）
    assert juya_daily._render_cache_path(latest, markdown + "\n") != text_cache
    print("render key changes with markdown: ok")

    # 不再对应缓存中正文的渲染图应被清理（两种模式的图都保留）
    stale = text_cache.with_name("stale_0.jpg")
    await asyncio.to_thread(stale.write_bytes, b"x")
    juya_daily._prune_render_cache()
    assert not stale.exists(), "陈旧渲染图未被清理"
    assert text_cache.is_file(), "清理误删了文字模式渲染图"
    assert img_cache.is_file(), "清理误删了配图模式渲染图"
    print("render cache prune: ok")

    out = "/tmp/juya_daily_latest.jpg"
    await asyncio.to_thread(Path(out).write_bytes, image)
    print("saved to:", out)


asyncio.run(main())
