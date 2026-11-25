"""橘鸦AI早报：抓取官方 markdown 源，整期渲染为一张长图

数据源为早报官方站点 daily.juya.uk（B站互动页面「橘鸦AI早报」的数据来源）：
- /issues.json：已发布期数列表（新→旧）
- /markdown/{date}.md：对应日期的早报 markdown

默认仅渲染文字（配置项 tool.juya_daily_render_images 关闭时直接移除配图行，
渲染不依赖图床且速度更快）；开启后先由 httpx 并发下载配图（带重试），再以
data URL 内联进 markdown，交给 nonebot_plugin_htmlrender 的 render_markdown
整体渲染为 JPEG 长图。配图托管在 assets.juya.uk，实测经本机网络加载缓慢且
不稳定（单张可达数十秒、偶发连接重置），直接让浏览器加载会拖垮渲染等待，
因此下载失败的配图也会从 markdown 中移除，渲染过程不访问网络。
markdown 源在段内使用硬换行（约 80 列），合并断行时仅在断点两侧均为字母数字
且至少一侧为 ASCII 时补空格，避免中文句中出现多余空格。
"""

import asyncio
import base64
import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

from nonebot import logger, require

from kanade_bot.utils.common import HTTPX_CLIENT, asia_shanghai_now
from kanade_bot.utils.persistence import atomic_write_bytes, atomic_write_text

from .config import cfg

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import render_markdown

ORIGIN = "https://daily.juya.uk"
"""早报官方站点"""

ISSUES_URL = f"{ORIGIN}/issues.json"
"""已发布期数列表接口（新→旧，响应头建议缓存 5 分钟）"""

ISSUES_TTL = 5 * 60.0
"""距上次确认期数列表不足该秒数时直接使用缓存"""

MARKDOWN_TTL = 30 * 60.0
"""单期正文距上次拉取不足该秒数时直接使用缓存（正文发布后偶有更正）"""

KEEP_MARKDOWN_COUNT = 7
"""持久化缓存保留的早报正文期数"""

IMAGE_CONCURRENCY = 8
"""配图并发下载数"""

IMAGE_ATTEMPTS = 3
"""单张配图下载尝试次数"""

IMAGE_TIMEOUT = 60.0
"""单张配图下载超时（秒），大图（如 GIF 动图）传输较慢"""

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
"""下载 assets.juya.uk 配图使用的浏览器 User-Agent"""

RENDER_WIDTH = 860
"""渲染视口宽度（CSS 像素）"""

RENDER_DEVICE_SCALE_FACTOR = 1.5
"""渲染设备像素比（长图总像素数与体积随该值增大，QQ 端过大易被压缩降质）"""

RENDER_JPEG_QUALITY = 88
"""渲染输出的 JPEG 质量"""

RENDER_TIMEOUT_SECONDS = 120.0
"""整体渲染超时（秒），需覆盖全部内联配图的解码与排版"""


def render_images_enabled() -> bool:
    """是否下载配图并渲染进长图（配置项 tool.juya_daily_render_images，默认仅渲染文字）"""
    return cfg.juya_daily_render_images


_issue_date_re = re.compile(r"\d{4}-\d{2}-\d{2}")
"""期数日期格式"""

_image_line_re = re.compile(r"(!\[[^\]]*\]\()(https?://[^)]+)(\))")
"""整行图片语法 `![alt](url)`，分组依次为前缀、URL、后缀"""

_image_url_re = re.compile(r"!\[[^\]]*\]\((https?://[^)\s]+)\)")
"""行内任意位置的图片引用，用于提取配图 URL"""

_video_url_re = re.compile(r"\[哔哩哔哩\]\((https?://www\.bilibili\.com/video/[^)\s]+)\)")
"""「视频版」行中的哔哩哔哩视频链接"""

_ref_re = re.compile(r"`#(\d+)`\s*$")
"""概览条目与正文标题行尾的条目编号 `` `#N` `` """


def markdown_url(issue_date: date) -> str:
    """某期早报的 markdown 源地址"""
    return f"{ORIGIN}/markdown/{issue_date:%Y-%m-%d}.md"


def issue_page_url(issue_date: date) -> str:
    """某期早报的官方详情页地址"""
    return f"{ORIGIN}/issues/{issue_date:%Y-%m-%d}/"


def parse_days_ago(text: str) -> int | None:
    """解析命令参数为回看天数（0 为当天），无法解析时返回 None"""
    text = text.strip()
    if text in ("", "0", "今天", "today"):
        return 0
    if text in ("1", "昨天", "yesterday"):
        return 1
    if matched := re.fullmatch(r"(\d{1,4})(?:天前|天)?", text):
        return int(matched.group(1))
    return None


def resolve_issue_date(issues: list[date], target: date) -> date | None:
    """目标日期对应的期数：精确命中，否则回退到该日之前最近的一期"""
    return next((issue for issue in issues if issue <= target), None)


# ---------- 抓取与缓存 ----------

_cache_file: Path = cfg.juya_daily_cache_file_path
"""持久化缓存文件（期数列表与最近几期正文）"""

_issues: list[date] = []
"""已发布期数（新→旧），重启后从缓存文件恢复"""

_issues_last_check: datetime | None = None
"""上次向接口确认期数列表的时间（无论成败，重启后从缓存文件恢复）"""

_markdowns: dict[date, tuple[datetime, str]] = {}
"""正文缓存：日期 -> (拉取时间, markdown)"""

_locks: dict[date, asyncio.Lock] = {}
"""各期的抓取与渲染锁：正文抓取与整期渲染共用，同一期并发请求去重
（asyncio.Lock 不可重入，两段逻辑必须先后获取，不得嵌套调用）"""

_issues_lock = asyncio.Lock()
"""期数列表刷新锁"""


def _load_cache() -> None:
    global _issues_last_check
    _issues.clear()
    _markdowns.clear()
    _issues_last_check = None
    if not _cache_file.exists():
        return
    try:
        data = json.loads(_cache_file.read_text(encoding="utf-8"))
        for value in data.get("issues") or []:
            if isinstance(value, str) and _issue_date_re.fullmatch(value):
                _issues.append(date.fromisoformat(value))
        # 注意 (x or {}).items() 的括号：不加括号时 .items() 只作用于字面量 {}
        for key, value in (data.get("markdowns") or {}).items():
            if not (
                isinstance(key, str) and isinstance(value, dict) and _issue_date_re.fullmatch(key)
            ):
                continue
            markdown = value.get("markdown")
            fetched_at = value.get("fetched_at")
            if isinstance(markdown, str) and isinstance(fetched_at, str):
                _markdowns[date.fromisoformat(key)] = (datetime.fromisoformat(fetched_at), markdown)
        if last_check := data.get("issues_last_check"):
            _issues_last_check = datetime.fromisoformat(last_check)
    except Exception:
        logger.exception(f"加载橘鸦AI早报缓存失败: {_cache_file}")
        _issues.clear()
        _markdowns.clear()
        _issues_last_check = None


def _save_cache() -> None:
    # 只保留最新几期，避免长期增长
    for issue_date in sorted(_markdowns)[:-KEEP_MARKDOWN_COUNT]:
        _markdowns.pop(issue_date)
    payload = {
        "issues_last_check": _issues_last_check.isoformat() if _issues_last_check else None,
        "issues": [issue.isoformat() for issue in _issues],
        "markdowns": {
            issue_date.isoformat(): {"fetched_at": fetched_at.isoformat(), "markdown": markdown}
            for issue_date, (fetched_at, markdown) in _markdowns.items()
        },
    }
    atomic_write_text(_cache_file, json.dumps(payload, ensure_ascii=False, indent=2))
    _prune_render_cache()


_load_cache()


async def _fetch_issues() -> list[date]:
    resp = await HTTPX_CLIENT.get(ISSUES_URL)
    resp.raise_for_status()
    issues = [
        date.fromisoformat(value)
        for item in resp.json().get("issues") or []
        if isinstance(value := item.get("date"), str) and _issue_date_re.fullmatch(value)
    ]
    issues.sort(reverse=True)
    return issues


def _issues_checked_recently() -> bool:
    return _issues_last_check is not None and (
        (asia_shanghai_now() - _issues_last_check).total_seconds() < ISSUES_TTL
    )


async def get_juya_issues() -> list[date]:
    """已发布期数列表（新→旧）

    距上次向接口确认不足 ISSUES_TTL 时直接使用缓存；确认失败时 TTL 内降级使用
    旧列表并防抖。
    """
    global _issues_last_check
    if _issues and _issues_checked_recently():
        return list(_issues)
    async with _issues_lock:
        if _issues and _issues_checked_recently():
            return list(_issues)
        try:
            fetched = await _fetch_issues()
        except Exception:
            logger.exception("获取橘鸦AI早报期数列表失败")
        else:
            if fetched:
                _issues[:] = fetched
            else:
                logger.warning("橘鸦AI早报期数列表为空")
        # 无论成败都记录确认时间：成功=已确认最新；失败=TTL 内降级旧列表并防抖
        _issues_last_check = asia_shanghai_now()
        _save_cache()
        return list(_issues)


async def _fetch_markdown(issue_date: date) -> str:
    resp = await HTTPX_CLIENT.get(markdown_url(issue_date))
    resp.raise_for_status()
    return resp.text


def _markdown_fresh(issue_date: date) -> bool:
    entry = _markdowns.get(issue_date)
    return entry is not None and (asia_shanghai_now() - entry[0]).total_seconds() < MARKDOWN_TTL


async def get_juya_daily_markdown(issue_date: date) -> str | None:
    """某期早报的 markdown 正文

    距上次拉取不足 MARKDOWN_TTL 时直接使用缓存；拉取失败时降级使用旧缓存，
    无缓存时返回 None。
    """
    if _markdown_fresh(issue_date):
        return _markdowns[issue_date][1]
    async with _locks.setdefault(issue_date, asyncio.Lock()):
        if _markdown_fresh(issue_date):
            return _markdowns[issue_date][1]
        try:
            markdown = await _fetch_markdown(issue_date)
        except Exception:
            logger.exception(f"获取橘鸦AI早报 {issue_date:%Y-%m-%d} 正文失败")
            if entry := _markdowns.get(issue_date):
                return entry[1]
            return None
        _markdowns[issue_date] = (asia_shanghai_now(), markdown)
        _save_cache()
        return markdown


# ---------- 配图下载与正文整理 ----------


async def download_juya_images(markdown: str) -> dict[str, tuple[bytes, str]]:
    """并发下载 markdown 引用的全部配图，返回 {url: (内容, Content-Type)}

    单张下载失败重试 IMAGE_ATTEMPTS 次后跳过（渲染时该图会被移除）。
    """
    urls = list(dict.fromkeys(_image_url_re.findall(markdown)))
    if not urls:
        return {}

    semaphore = asyncio.Semaphore(IMAGE_CONCURRENCY)
    images: dict[str, tuple[bytes, str]] = {}

    async def fetch(url: str) -> None:
        async with semaphore:
            for attempt in range(1, IMAGE_ATTEMPTS + 1):
                try:
                    resp = await HTTPX_CLIENT.get(
                        url,
                        headers={"User-Agent": BROWSER_UA},
                        follow_redirects=True,
                        timeout=IMAGE_TIMEOUT,
                    )
                    resp.raise_for_status()
                    content_type = (resp.headers.get("content-type") or "").split(";")[0].strip()
                    images[url] = (resp.content, content_type)
                    return
                except Exception:
                    if attempt == IMAGE_ATTEMPTS:
                        logger.warning(f"下载橘鸦AI早报配图失败（已尝试{IMAGE_ATTEMPTS}次）: {url}")

    await asyncio.gather(*(fetch(url) for url in urls))
    return images


def _join_hard_wrap(left: str, right: str) -> str:
    """硬换行断点处的连接符：两侧均为字母数字且至少一侧为 ASCII 时补空格"""
    if left.isalnum() and right.isalnum() and (left.isascii() or right.isascii()):
        return " "
    return ""


def _is_own_block(line: str) -> bool:
    """该行是否自成一块（空行/标题/引用/列表/分隔线/代码围栏/整行图片）"""
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith(("#", ">", "- ", "* ", "```", "---"))
        or _image_line_re.fullmatch(stripped) is not None
    )


def _reflow_markdown(markdown: str) -> str:
    """合并段内硬换行（markdown 源约 80 列换行），引用行保留 ``> `` 前缀"""
    out: list[str] = []
    buf: list[str] = []
    in_quote = False
    in_code = False

    def flush() -> None:
        if not buf:
            return
        joined = buf[0]
        for line in buf[1:]:
            joined += _join_hard_wrap(joined[-1], line[0]) + line
        out.append(f"> {joined}" if in_quote else joined)
        buf.clear()

    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            flush()
            out.append(line)
            continue
        if in_code:
            out.append(line)
            continue
        if line.startswith(">"):
            content = line.lstrip(">").strip()
            if content:
                if buf and not in_quote:
                    flush()
                in_quote = True
                buf.append(content)
            else:
                flush()
            continue
        if _is_own_block(line):
            flush()
            in_quote = False
            out.append(line)
            continue
        # 引用段的懒惰延续行（硬换行的续行无 > 前缀）同样并入当前引用
        buf.append(line.strip())
    flush()
    return "\n".join(out)


async def _prepare_markdown(markdown: str) -> str:
    """合并硬换行，并按配置处理配图

    开启 tool.juya_daily_render_images 时下载配图并以 data URL 内联（不可用的图
    移除）；关闭时直接移除全部配图行，仅渲染文字。
    """
    reflowed = _reflow_markdown(markdown).splitlines()
    if not render_images_enabled():
        return "\n".join(
            line for line in reflowed if _image_line_re.fullmatch(line.strip()) is None
        )

    images = await download_juya_images(markdown)
    lines: list[str] = []
    for line in reflowed:
        matched = _image_line_re.fullmatch(line.strip())
        if matched is None:
            lines.append(line)
            continue
        url = matched.group(2)
        image = images.get(url)
        if image is None or not image[0] or not image[1].startswith("image/"):
            logger.warning(f"橘鸦AI早报配图不可用，渲染时跳过: {url}")
            continue
        content, content_type = image
        encoded = base64.b64encode(content).decode("ascii")
        lines.append(f"{matched.group(1)}data:{content_type};base64,{encoded}{matched.group(3)}")
    return "\n".join(lines)


# ---------- 渲染 ----------

_render_cache_dir: Path = cfg.juya_daily_image_cache_dir_path
"""渲染图缓存目录"""


def _render_cache_path(issue_date: date, markdown: str, *, images: bool | None = None) -> Path:
    """渲染图缓存路径：文件名含日期、正文摘要、渲染模式与参数，正文更正或配置变更后自动失效

    images 为 None 时取当前配置（tool.juya_daily_render_images）。
    """
    digest = hashlib.sha1(markdown.encode("utf-8")).hexdigest()[:12]
    if images is None:
        images = render_images_enabled()
    mode = "img" if images else "text"
    render_key = f"{RENDER_WIDTH}x{RENDER_DEVICE_SCALE_FACTOR:g}q{RENDER_JPEG_QUALITY}_{mode}"
    return _render_cache_dir / f"{issue_date:%Y-%m-%d}_{digest}_{render_key}.jpg"


def is_render_cached(issue_date: date, markdown: str) -> bool:
    """该期早报的渲染长图是否已缓存（未缓存时开启配图渲染首次约需一分钟）"""
    return _render_cache_path(issue_date, markdown).is_file()


async def render_juya_daily(issue_date: date, markdown: str) -> bytes:
    """将整期早报渲染为 JPEG 长图（按日期、正文与渲染模式缓存，命中时不再渲染）

    开启配图渲染时先下载再以 data URL 内联，渲染过程不访问网络；同一期的并发
    渲染共用该期锁去重。
    """
    cache_path = _render_cache_path(issue_date, markdown)
    if cache_path.is_file():
        return cache_path.read_bytes()

    async with _locks.setdefault(issue_date, asyncio.Lock()):
        cache_path = _render_cache_path(issue_date, markdown)
        if cache_path.is_file():
            return cache_path.read_bytes()
        prepared = await _prepare_markdown(markdown)
        rendered = await render_markdown(
            prepared,
            width=RENDER_WIDTH,
            device_pixel_ratio=RENDER_DEVICE_SCALE_FACTOR,
            image_format="jpeg",
            quality=RENDER_JPEG_QUALITY,
            timeout_seconds=RENDER_TIMEOUT_SECONDS,
        )
        atomic_write_bytes(cache_path, rendered.data)
        _prune_render_cache()
        return rendered.data


def _prune_render_cache() -> None:
    """清理不再对应缓存中正文的渲染图（含旧版本正文与过期期数的图）

    同期正文同时保留文字与配图两种模式的渲染图，切换配置后无需重新渲染。
    """
    if not _markdowns or not _render_cache_dir.is_dir():
        return
    expected = {
        _render_cache_path(issue_date, markdown, images=images).name
        for issue_date, (_, markdown) in _markdowns.items()
        for images in (False, True)
    }
    for file in _render_cache_dir.iterdir():
        if file.name not in expected:
            file.unlink(missing_ok=True)


# ---------- 概览与链接提取（渲染失败时的降级内容） ----------


def _strip_inline(text: str) -> str:
    """去除行内 markdown 标记（链接取文字，去 ↗、粗体与行内代码）"""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.replace("↗", "")
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    return text.strip()


def extract_video_url(markdown: str) -> str | None:
    """从「视频版」行提取哔哩哔哩视频链接"""
    return matched.group(1) if (matched := _video_url_re.search(markdown)) else None


def overview_text(markdown: str) -> str:
    """提取「概览」小节为纯文本（渲染失败时的降级内容）"""
    groups: list[tuple[str, list[str]]] = []
    in_overview = False
    for raw in markdown.splitlines():
        line = raw.strip()
        if not in_overview:
            in_overview = line.startswith("## ") and "概览" in line
            continue
        if line.startswith("---"):
            break
        if line.startswith("### "):
            groups.append((line[4:].strip(), []))
        elif line.startswith("- ") and groups:
            groups[-1][1].append(line[2:].strip())
    if not groups:
        return ""

    parts: list[str] = []
    for category, items in groups:
        section = [f"【{category}】"]
        for item in items:
            ref = _ref_re.search(item)
            number = f"{ref.group(1)}. " if ref else "- "
            section.append(number + _strip_inline(_ref_re.sub("", item)))
        parts.append("\n".join(section))
    return "\n\n".join(parts)
