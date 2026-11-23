"""显卡日报：定位知乎专栏最新文章，取其正文 HTML 自行渲染为图片

数据源为知乎移动端接口（免登录）：列表按发布时间倒序，标题含「显卡日报」的条目即日报。
发布者每天最多两篇（中午偶尔加更 + 23:00 左右日常必发），发布时刻不固定，
因此新鲜度以「距上次向接口确认不足 CHECK_TTL」为准，而非按发布时刻表推断；
同时缓存最近多期，支持回看上一期。
正文中的图片为知乎图床外链，仅校验 User-Agent（渲染时的 Chromium 天然满足）。
"""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from httpx import AsyncClient
from nonebot import logger, require
from pydantic import BaseModel

from kanade_bot.utils.common import HTTPX_CLIENT, asia_shanghai_now
from kanade_bot.utils.persistence import atomic_write_text

from .config import cfg

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import get_default_application

COLUMN_ID = "c_1433942042578157568"
"""发布显卡日报的知乎专栏 id"""

COLUMN_URL = f"https://www.zhihu.com/column/{COLUMN_ID}"
"""显卡日报专栏主页"""

API_URL = f"https://api.zhihu.com/columns/{COLUMN_ID}/items"
"""专栏文章列表接口（免登录，按发布时间倒序）"""

TITLE_KEYWORD = "显卡日报"
"""用于在专栏列表中识别日报文章的标题关键字"""

MOBILE_UA = "osee2unifiedRelease/22.5.0 (iPhone; iOS 16.5; Scale/3.00)"
"""访问知乎接口使用的移动端 User-Agent（网页端接口有 JS 挑战，移动端免登录）"""

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""知乎图床仅放行浏览器 User-Agent，下载正文图片时使用"""

CHECK_TTL = 30 * 60.0
"""距上次向接口确认的时长小于该值时直接使用缓存（秒），兼作接口失败后的防抖间隔"""

FETCH_LIMIT = 10
"""单次请求拉取并缓存的文章条数（一天最多两篇，可覆盖数天）"""

MAX_LOOKBACK = 5
"""命令允许回看的最大期数（0 为最新一期）"""

KEEP_COUNT = 7
"""持久化缓存保留的日报期数"""

RENDER_VIEWPORT_WIDTH = 860
"""渲染视口宽度（CSS 像素）"""

RENDER_DEVICE_SCALE_FACTOR = 2.0
"""渲染设备像素比"""

RENDER_JPEG_QUALITY = 87
"""渲染输出的 JPEG 质量"""

_api_client: AsyncClient | None = None
"""知乎接口专用客户端（首次请求时懒创建，复用以稳定携带访客 cookie）"""


class GpuDailyReport(BaseModel):
    """显卡日报"""

    article_id: str
    """知乎文章 id"""
    title: str
    """文章标题"""
    url: str
    """文章链接"""
    author: str
    """作者名"""
    excerpt: str
    """文章摘要"""
    created: datetime
    """发布时间（上海时区）"""
    updated: datetime
    """最后编辑时间（上海时区）"""
    content: str
    """正文 HTML（知乎图床外链原样保留）"""


def _build_report(item: dict) -> GpuDailyReport:
    """由接口条目构造 GpuDailyReport"""
    tz = ZoneInfo("Asia/Shanghai")
    # 防御性移除正文中的脚本/样式标签（正常文章不会有）
    content = re.sub(
        r"<(script|style)\b[^>]*>.*?</\1>",
        "",
        item.get("content") or "",
        flags=re.DOTALL | re.IGNORECASE,
    )
    if item.get("content_need_truncated"):
        logger.debug(f"显卡日报 {item.get('id')} 接口标记可能截断，实际内容长度: {len(content)}")
    return GpuDailyReport(
        article_id=str(item.get("id") or ""),
        title=item.get("title") or "",
        url=item.get("url") or f"https://zhuanlan.zhihu.com/p/{item.get('id')}",
        author=(item.get("author") or {}).get("name") or "",
        excerpt=(item.get("excerpt") or "").strip(),
        created=datetime.fromtimestamp(item["created"], tz=tz),
        updated=datetime.fromtimestamp(item["updated"], tz=tz),
        content=content,
    )


async def _fetch_latest_reports() -> list[GpuDailyReport]:
    """请求专栏列表接口，返回最近的日报列表（倒序，跳过少量非日报文章）

    使用专用客户端：与全局客户端隔离 cookie（知乎会下发 _xsrf/BEC 等访客 cookie，
    稳定携带可降低被限流概率），UA 固定为移动端。
    """
    global _api_client
    if _api_client is None:
        _api_client = AsyncClient(timeout=20, headers={"User-Agent": MOBILE_UA})
    resp = await _api_client.get(API_URL, params={"limit": FETCH_LIMIT, "offset": 0})
    resp.raise_for_status()
    return [
        _build_report(item)
        for item in resp.json().get("data") or []
        if TITLE_KEYWORD in (item.get("title") or "")
    ]


# ---------- 缓存 ----------

_cache_file: Path = cfg.gpu_daily_cache_file_path
"""持久化缓存文件（含 last_check 与最近几期日报）"""
_cache: dict[str, GpuDailyReport] = {}
_last_check: datetime | None = None
"""上次向接口确认（无论成败）的时间，重启后从缓存文件恢复"""
_fetch_lock = asyncio.Lock()


def _load_cache() -> None:
    global _last_check
    _cache.clear()
    _last_check = None
    if not _cache_file.exists():
        return
    try:
        data = json.loads(_cache_file.read_text(encoding="utf-8"))
        # 兼容旧格式（纯 {id: report} 字典，无 last_check）
        reports = data.get("reports", data)
        _cache.update({key: GpuDailyReport.model_validate(value) for key, value in reports.items()})
        if last := data.get("last_check"):
            _last_check = datetime.fromisoformat(last)
    except Exception:
        logger.exception(f"加载显卡日报缓存失败: {_cache_file}")
        _cache.clear()
        _last_check = None


def _save_cache() -> None:
    # 只保留最新几期，避免长期增长
    newest = sorted(_cache.values(), key=lambda r: r.created, reverse=True)[:KEEP_COUNT]
    _cache.clear()
    _cache.update({r.article_id: r for r in newest})
    payload = {
        "last_check": _last_check.isoformat() if _last_check else None,
        "reports": {key: value.model_dump(mode="json") for key, value in _cache.items()},
    }
    atomic_write_text(_cache_file, json.dumps(payload, ensure_ascii=False, indent=2))


_load_cache()


def _sorted_reports() -> list[GpuDailyReport]:
    """缓存中的日报，按发布时间倒序（第 0 期最新）"""
    return sorted(_cache.values(), key=lambda r: r.created, reverse=True)


def _checked_recently(now: datetime) -> bool:
    """距上次接口确认是否在 TTL 内"""
    return _last_check is not None and (now - _last_check).total_seconds() < CHECK_TTL


async def get_gpu_daily_reports() -> list[GpuDailyReport]:
    """获取最近几期显卡日报（倒序，第 0 期最新）

    距上次接口确认不足 CHECK_TTL 时直接使用缓存，否则请求一次接口刷新。
    发布时刻不固定（中午加更/晚间日常），因此不按时刻表推断新鲜度。
    """
    global _last_check
    now = asia_shanghai_now()
    if _cache and _checked_recently(now):
        return _sorted_reports()

    async with _fetch_lock:
        now = asia_shanghai_now()  # 双重检查，避免并发重复抓取
        if _cache and _checked_recently(now):
            return _sorted_reports()
        try:
            fetched = await _fetch_latest_reports()
        except Exception:
            logger.exception("获取显卡日报接口失败")
        else:
            if fetched:
                _cache.update({r.article_id: r for r in fetched})
            else:
                logger.warning(f"显卡日报接口返回数据中未找到含「{TITLE_KEYWORD}」的文章")
        # 无论成败都记录确认时间：成功=已确认最新；失败=TTL 内降级使用缓存并防抖
        _last_check = now
        _save_cache()
        return _sorted_reports()


# ---------- 渲染 ----------

_IMG_SRC_RE = re.compile(r"<img\b[^>]*?\bsrc=\"([^\"]+)\"", re.IGNORECASE)

_TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    background: #ffffff;
    font-family: "PingFang SC", "HarmonyOS Sans SC", "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  }
  .article { max-width: 100%; padding: 26px 22px 18px; }
  h1 {
    font-size: 23px;
    line-height: 1.45;
    color: #1a1a1a;
    font-weight: 600;
    margin-bottom: 10px;
  }
  .meta {
    font-size: 13px;
    color: #909399;
    margin-bottom: 18px;
    padding-bottom: 14px;
    border-bottom: 1px solid #ebeef5;
  }
  .content p {
    font-size: 16.5px;
    line-height: 1.8;
    color: #2f3a4f;
    margin: 0 0 14px;
  }
  .content figure { margin: 0 0 14px; }
  .content img {
    display: block;
    max-width: 100%;
    width: auto;
    height: auto;
    border-radius: 4px;
    margin: 0 auto 4px;
  }
  .content figcaption {
    font-size: 13px;
    color: #909399;
    text-align: center;
    margin-top: 4px;
  }
  .content a { color: #2f6fbd; text-decoration: none; word-break: break-all; }
  .content blockquote {
    margin: 0 0 14px;
    padding: 8px 14px;
    border-left: 3px solid #dcdfe6;
    background: #f7f8fa;
    color: #5a5e66;
    font-size: 15px;
    line-height: 1.7;
  }
  .content hr { border: none; border-top: 1px solid #ebeef5; margin: 16px 0; }
  .footer {
    margin-top: 20px;
    padding-top: 12px;
    border-top: 1px solid #ebeef5;
    font-size: 12.5px;
    color: #a8abb2;
    line-height: 1.6;
    word-break: break-all;
  }
</style>
</head>
<body>
  <div class="article">
    <h1>{{ title }}</h1>
    <div class="meta">{{ author }} · {{ created }} · 来自知乎专栏</div>
    <div class="content">{{ content }}</div>
    <div class="footer">原文：{{ url }}</div>
  </div>
</body>
</html>
"""


def _build_html(report: GpuDailyReport) -> str:
    """将日报填入渲染模板（正文为原样 HTML，占位符手工替换避免转义）"""
    replacements = {
        "title": report.title,
        "author": report.author,
        "created": f"{report.created:%Y-%m-%d %H:%M}",
        "content": report.content,
        "url": report.url,
    }
    html = _TEMPLATE
    for key, value in replacements.items():
        html = html.replace("{{ " + key + " }}", value)
    return html


async def render_gpu_daily(report: GpuDailyReport) -> bytes:
    """将日报渲染为 JPEG 图片（浏览器直接加载正文中的知乎图床外链）"""
    html = _build_html(report)
    app = get_default_application()
    async with app.extensions.playwright.page(
        viewport={"width": RENDER_VIEWPORT_WIDTH, "height": 800},
        device_scale_factor=RENDER_DEVICE_SCALE_FACTOR,
    ) as page:
        # networkidle 等待正文外链图片全部加载完成
        await page.set_content(html, wait_until="networkidle")
        return await page.screenshot(full_page=True, type="jpeg", quality=RENDER_JPEG_QUALITY)


async def download_gpu_daily_images(report: GpuDailyReport) -> list[bytes]:
    """下载正文中的全部图片内容（渲染失败时的降级发送路径）"""
    urls: list[str] = []
    for matched in _IMG_SRC_RE.finditer(report.content):
        url = matched.group(1)
        if url not in urls:
            urls.append(url)

    images: list[bytes] = []
    for url in urls:
        resp = await HTTPX_CLIENT.get(
            url, headers={"User-Agent": BROWSER_UA}, follow_redirects=True
        )
        resp.raise_for_status()
        images.append(resp.content)
    return images
