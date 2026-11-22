"""网络安全日报/周报：定位 B 站动态并取其图片与置顶评论链接"""

import asyncio
import json
import re
import time
from datetime import date, timedelta
from pathlib import Path

from bilibili_api import Credential
from bilibili_api import comment as bili_comment
from bilibili_api import opus as bili_opus
from bilibili_api import user as bili_user
from bilibili_api.comment import CommentResourceType
from nonebot import logger, require
from pydantic import BaseModel

from kanade_bot.utils.common import HTTPX_CLIENT, asia_shanghai_now
from kanade_bot.utils.persistence import atomic_write_text

from .config import cfg

require("nonebot_plugin_parser")
from nonebot_plugin_parser.matchers import get_parser_by_type
from nonebot_plugin_parser.parsers.bilibili import BilibiliParser

SECURITY_UID = 72338055
"""发布网络安全日报/周报的 B 站用户 uid"""

DAILY_TITLE = "#网络安全日报#"
WEEKLY_TITLE = "#网络安全周报#"
WEEKDAY_ZH = "一二三四五六日"
MAX_PAGES = 10
"""翻页查找动态的最大页数"""
MISS_CACHE_TTL = 15 * 60.0
"""未命中/确认无更新时的内存负缓存时长（秒），避免高频重复翻页"""

IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
"""下载日报/周报图片使用的请求头"""


# ---------- 共用 ----------


class SecurityDaily(BaseModel):
    """网络安全日报"""

    date: date
    """日报日期"""
    dynamic_id: str
    """动态 id"""
    dynamic_url: str
    """动态链接"""
    text: str
    """动态正文"""
    images: list[str]
    """动态图片 url 列表"""
    top_comment: str | None = None
    """置顶评论原文"""
    top_comment_url: str | None = None
    """从置顶评论中提取的第一个 url"""


class SecurityWeekly(BaseModel):
    """网络安全周报"""

    start_date: date
    """周报覆盖起始日期"""
    end_date: date
    """周报覆盖截止日期"""
    dynamic_id: str
    """动态 id"""
    dynamic_url: str
    """动态链接"""
    text: str
    """动态正文"""
    images: list[str]
    """动态图片 url 列表"""


def _dynamic_text(item: dict) -> str:
    """提取动态自身正文文本（兼容 opus.summary / desc.text / draw.items.description）"""
    md = (item.get("modules") or {}).get("module_dynamic") or {}
    chunks: list[str] = []
    if md.get("desc"):
        chunks.append(md["desc"].get("text") or "")
    major = md.get("major") or {}
    if major.get("opus"):
        chunks.append((major["opus"].get("summary") or {}).get("text") or "")
    if major.get("draw"):
        for pic in major["draw"].get("items") or []:
            chunks.append(str(pic.get("description") or ""))
    return "\n".join(c for c in chunks if c)


def _dynamic_images(item: dict) -> list[str]:
    """提取动态图片 url（兼容 opus.pics 与 draw.items 两种结构）"""
    md = (item.get("modules") or {}).get("module_dynamic") or {}
    major = md.get("major") or {}
    urls: list[str] = []
    for pic in (major.get("opus") or {}).get("pics") or []:
        if pic.get("url"):
            urls.append(pic["url"])
    for pic in (major.get("draw") or {}).get("items") or []:
        if pic.get("src"):
            urls.append(pic["src"])
    return urls


async def _get_credential() -> Credential | None:
    """复用 parser 插件 BilibiliParser 实例的登录凭证（含有效性检查与自动刷新）"""
    try:
        parser = get_parser_by_type(BilibiliParser)
    except ValueError:
        logger.warning("parser 插件未启用 B 站解析器，将以未登录状态获取网络安全日报/周报")
        return None
    try:
        return await parser.credential
    except Exception:
        logger.exception("获取 B 站登录凭证失败，将以未登录状态获取网络安全日报/周报")
        return None


async def fetch_top_comment(
    dynamic_id: str, basic: dict, credential: Credential | None
) -> tuple[str | None, str | None]:
    """获取动态置顶评论原文与其中的第一个 url（获取失败时降级为 None）"""
    # 评论区 rid/type 优先取动态 basic 自带字段，省一次 opus.get_rid() 请求
    rid = basic.get("comment_id_str") or basic.get("rid_str") or ""
    if rid:
        comment_type = CommentResourceType(
            basic.get("comment_type") or CommentResourceType.DYNAMIC_DRAW.value
        )
    else:
        rid = await bili_opus.Opus(int(dynamic_id), credential=credential).get_rid()
        comment_type = CommentResourceType.DYNAMIC_DRAW

    top_comment: str | None = None
    try:
        comments = await bili_comment.get_comments_lazy(
            int(rid), comment_type, credential=credential
        )
        upper = (comments.get("top") or {}).get("upper") or {}
        top_comment = ((upper.get("content") or {}).get("message") or "").strip() or None
    except Exception:
        logger.exception(f"获取动态 {dynamic_id} 的置顶评论失败")

    top_comment_url = None
    if top_comment and (matched := re.search(r"https?://\S+", top_comment)):
        top_comment_url = matched.group(0)
    return top_comment, top_comment_url


async def download_report_images(urls: list[str]) -> list[bytes]:
    """下载动态图片内容"""
    images: list[bytes] = []
    for url in urls:
        resp = await HTTPX_CLIENT.get(url, headers=IMG_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        images.append(resp.content)
    return images


# ---------- 日报 ----------

DAILY_KEEP_DAYS = 7
"""持久化缓存保留的日报天数"""
DAILY_FALLBACK_DAYS = 7
"""当天未发布时向前回退查找的最大天数（周末或节假日可能断更）"""


def _daily_pattern(target: date) -> re.Pattern:
    """构造「#网络安全日报#2026年11月18日，星期三」形式的正则（月/日容忍补零写法）"""
    return re.compile(
        rf"{DAILY_TITLE}{target.year}年0?{target.month}月0?{target.day}日"
        rf"，星期{WEEKDAY_ZH[target.weekday()]}"
    )


async def _build_daily(
    target: date, item: dict, text: str, credential: Credential | None
) -> SecurityDaily:
    """由动态条目构造 SecurityDaily（置顶评论获取失败时降级为无评论）"""
    dynamic_id = item.get("id_str") or str(item.get("id") or "")
    top_comment, top_comment_url = await fetch_top_comment(
        dynamic_id, item.get("basic") or {}, credential
    )

    return SecurityDaily(
        date=target,
        dynamic_id=dynamic_id,
        dynamic_url=f"https://www.bilibili.com/opus/{dynamic_id}",
        text=text.strip(),
        images=_dynamic_images(item),
        top_comment=top_comment,
        top_comment_url=top_comment_url,
    )


async def _fetch_security_daily(
    target: date, credential: Credential | None
) -> SecurityDaily | None:
    """翻页遍历动态，抓取指定日期的网络安全日报"""
    u = bili_user.User(SECURITY_UID, credential=credential)
    pattern = _daily_pattern(target)

    offset = ""
    for _ in range(MAX_PAGES):
        page = await u.get_dynamics_new(offset)
        for item in page.get("items") or []:
            text = _dynamic_text(item)
            if not pattern.search(text):
                continue
            return await _build_daily(target, item, text, credential)

        if str(page.get("has_more")).lower() not in ("1", "true"):
            break
        offset = page.get("offset") or ""
    return None


_daily_cache_file: Path = cfg.security_daily_cache_file_path
"""日报持久化缓存文件（键为日期 iso 格式）"""
_daily_cache: dict[str, SecurityDaily] = {}
_daily_miss_cache: dict[str, float] = {}
_daily_fetch_lock = asyncio.Lock()


def _load_daily_cache() -> None:
    if not _daily_cache_file.exists():
        return
    try:
        data = json.loads(_daily_cache_file.read_text(encoding="utf-8"))
        _daily_cache.update(
            {key: SecurityDaily.model_validate(value) for key, value in data.items()}
        )
    except Exception:
        logger.exception(f"加载网络安全日报缓存失败: {_daily_cache_file}")
        _daily_cache.clear()


def _save_daily_cache() -> None:
    cutoff = (asia_shanghai_now() - timedelta(days=DAILY_KEEP_DAYS)).date().isoformat()
    for key in [k for k in _daily_cache if k < cutoff]:
        del _daily_cache[key]
    payload = {key: value.model_dump(mode="json") for key, value in _daily_cache.items()}
    atomic_write_text(_daily_cache_file, json.dumps(payload, ensure_ascii=False, indent=2))


_load_daily_cache()


async def _get_or_fetch_daily(target: date) -> SecurityDaily | None:
    """带缓存地获取指定日期的日报，未发布时记录负缓存"""
    key = target.isoformat()
    if daily := _daily_cache.get(key):
        return daily

    miss_ts = _daily_miss_cache.get(key)
    if miss_ts is not None and time.monotonic() - miss_ts < MISS_CACHE_TTL:
        return None

    async with _daily_fetch_lock:
        if daily := _daily_cache.get(key):  # 双重检查，避免并发重复抓取
            return daily
        credential = await _get_credential()
        if credential is None:
            logger.warning(
                "未获取到 B 站登录凭证，动态接口可能不会返回数据（请配置 parser_bili_ck 或使用 blogin 扫码登录）"
            )
        daily = await _fetch_security_daily(target, credential)
        if daily is not None:
            _daily_cache[key] = daily
            _save_daily_cache()
        else:
            _daily_miss_cache[key] = time.monotonic()
            logger.info(f"未找到 {target} 的网络安全日报（可能尚未发布）")
        return daily


async def get_latest_security_daily() -> SecurityDaily | None:
    """获取最新一期日报：优先当天，未发布时向前回退找最近一期"""
    today = asia_shanghai_now().date()
    for delta in range(DAILY_FALLBACK_DAYS):
        if daily := await _get_or_fetch_daily(today - timedelta(days=delta)):
            return daily
    return None


async def download_daily_images(daily: SecurityDaily) -> list[bytes]:
    """下载日报的全部图片内容"""
    return await download_report_images(daily.images)


# ---------- 周报 ----------

WEEKLY_KEEP_DAYS = 21
"""持久化缓存保留的周报天数（约 3 期）"""
WEEKLY_STALE_DAYS = 7
"""缓存的最新一期周报覆盖截止日期距今天超过该天数时视为过期，需翻页确认是否有更新"""

_WEEKLY_RANGE_RE = re.compile(
    rf"{WEEKLY_TITLE}[\s\u200b]*"
    r"(\d{4})年(\d{1,2})月(\d{1,2})日[\s\u200b]*[-–—~～－至][\s\u200b]*"
    r"(?:(\d{4})年)?(\d{1,2})月(\d{1,2})日"
)


def _parse_weekly_range(text: str) -> tuple[date, date] | None:
    """解析「#网络安全周报#2026年11月13日-2026年11月19日」形式的日期区间（容忍补零写法）"""
    matched = _WEEKLY_RANGE_RE.search(text)
    if not matched:
        return None
    y1, m1, d1, y2, m2, d2 = matched.groups()
    try:
        start = date(int(y1), int(m1), int(d1))
        end = date(int(y2 or y1), int(m2), int(d2))
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _build_weekly(item: dict, text: str, rng: tuple[date, date]) -> SecurityWeekly:
    """由动态条目构造 SecurityWeekly"""
    dynamic_id = item.get("id_str") or str(item.get("id") or "")
    return SecurityWeekly(
        start_date=rng[0],
        end_date=rng[1],
        dynamic_id=dynamic_id,
        dynamic_url=f"https://www.bilibili.com/opus/{dynamic_id}",
        text=text.strip(),
        images=_dynamic_images(item),
    )


async def _fetch_security_weekly(
    credential: Credential | None,
) -> SecurityWeekly | None:
    """翻页遍历动态，抓取最新一期网络安全周报（动态按时间倒序，首个命中即最新）"""
    u = bili_user.User(SECURITY_UID, credential=credential)

    offset = ""
    for _ in range(MAX_PAGES):
        page = await u.get_dynamics_new(offset)
        for item in page.get("items") or []:
            text = _dynamic_text(item)
            if rng := _parse_weekly_range(text):
                return _build_weekly(item, text, rng)

        if str(page.get("has_more")).lower() not in ("1", "true"):
            break
        offset = page.get("offset") or ""
    return None


_weekly_cache_file: Path = cfg.security_weekly_cache_file_path
"""周报持久化缓存文件（键为周报覆盖截止日期 iso 格式）"""
_weekly_cache: dict[str, SecurityWeekly] = {}
_weekly_miss_ts: float | None = None
_weekly_fetch_lock = asyncio.Lock()


def _load_weekly_cache() -> None:
    if not _weekly_cache_file.exists():
        return
    try:
        data = json.loads(_weekly_cache_file.read_text(encoding="utf-8"))
        _weekly_cache.update(
            {key: SecurityWeekly.model_validate(value) for key, value in data.items()}
        )
    except Exception:
        logger.exception(f"加载网络安全周报缓存失败: {_weekly_cache_file}")
        _weekly_cache.clear()


def _save_weekly_cache() -> None:
    cutoff = (asia_shanghai_now() - timedelta(days=WEEKLY_KEEP_DAYS)).date().isoformat()
    for key in [k for k in _weekly_cache if k < cutoff]:
        del _weekly_cache[key]
    payload = {key: value.model_dump(mode="json") for key, value in _weekly_cache.items()}
    atomic_write_text(_weekly_cache_file, json.dumps(payload, ensure_ascii=False, indent=2))


def _newest_weekly_cached() -> SecurityWeekly | None:
    """缓存中覆盖截止日期最新的一期（键为 end_date iso 格式，可直接取 max）"""
    return _weekly_cache[max(_weekly_cache)] if _weekly_cache else None


_load_weekly_cache()


async def get_latest_security_weekly() -> SecurityWeekly | None:
    """获取最新一期周报：缓存的最新一期未过期时直接使用，否则翻页确认是否有更新"""
    global _weekly_miss_ts
    today = asia_shanghai_now().date()
    weekly = _newest_weekly_cached()
    if weekly and (today - weekly.end_date).days <= WEEKLY_STALE_DAYS:
        return weekly
    if _weekly_miss_ts is not None and time.monotonic() - _weekly_miss_ts < MISS_CACHE_TTL:
        return weekly

    async with _weekly_fetch_lock:
        weekly = _newest_weekly_cached()  # 双重检查，避免并发重复抓取
        if weekly and (today - weekly.end_date).days <= WEEKLY_STALE_DAYS:
            return weekly
        credential = await _get_credential()
        if credential is None:
            logger.warning(
                "未获取到 B 站登录凭证，动态接口可能不会返回数据（请配置 parser_bili_ck 或使用 blogin 扫码登录）"
            )
        weekly = await _fetch_security_weekly(credential)
        if weekly is not None:
            _weekly_cache[weekly.end_date.isoformat()] = weekly
            _save_weekly_cache()
        if weekly is None or (today - weekly.end_date).days > WEEKLY_STALE_DAYS:
            _weekly_miss_ts = time.monotonic()
            logger.info("未找到更新的网络安全周报（可能尚未发布）")
        return weekly


async def download_weekly_images(weekly: SecurityWeekly) -> list[bytes]:
    """下载周报的全部图片内容"""
    return await download_report_images(weekly.images)
