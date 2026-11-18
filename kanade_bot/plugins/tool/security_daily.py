"""网络安全日报：定位 B 站动态并取其图片与置顶评论链接"""

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

SECURITY_DAILY_UID = 72338055
"""发布网络安全日报的 B 站用户 uid"""

DAILY_TITLE = "#网络安全日报#"
WEEKDAY_ZH = "一二三四五六日"
MAX_PAGES = 10
"""翻页查找动态的最大页数"""
MISS_CACHE_TTL = 15 * 60.0
"""指定日期未发布时的内存负缓存时长（秒），避免高频重复翻页"""
KEEP_DAYS = 7
"""持久化缓存保留的日报天数"""
FALLBACK_DAYS = 7
"""当天未发布时向前回退查找的最大天数（周末或节假日可能断更）"""

IMG_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
}
"""下载日报图片使用的请求头"""


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


def _daily_pattern(target: date) -> re.Pattern:
    """构造「#网络安全日报#2026年11月18日，星期三」形式的正则（月/日容忍补零写法）"""
    return re.compile(
        rf"{DAILY_TITLE}{target.year}年0?{target.month}月0?{target.day}日"
        rf"，星期{WEEKDAY_ZH[target.weekday()]}"
    )


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
        logger.warning("parser 插件未启用 B 站解析器，将以未登录状态获取网络安全日报")
        return None
    try:
        return await parser.credential
    except Exception:
        logger.exception("获取 B 站登录凭证失败，将以未登录状态获取网络安全日报")
        return None


async def _build_daily(
    target: date, item: dict, text: str, credential: Credential | None
) -> SecurityDaily:
    """由动态条目构造 SecurityDaily（置顶评论获取失败时降级为无评论）"""
    dynamic_id = item.get("id_str") or str(item.get("id") or "")
    basic = item.get("basic") or {}

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
        logger.exception(f"获取 {target} 网络安全日报的置顶评论失败")

    top_comment_url = None
    if top_comment and (matched := re.search(r"https?://\S+", top_comment)):
        top_comment_url = matched.group(0)

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
    u = bili_user.User(SECURITY_DAILY_UID, credential=credential)
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


_CACHE_FILE: Path = cfg.security_daily_cache_file_path
"""持久化缓存文件（键为日期 iso 格式）"""
_cache: dict[str, SecurityDaily] = {}
_miss_cache: dict[str, float] = {}
_fetch_lock = asyncio.Lock()


def _load_cache() -> None:
    if not _CACHE_FILE.exists():
        return
    try:
        data = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        _cache.update({key: SecurityDaily.model_validate(value) for key, value in data.items()})
    except Exception:
        logger.exception(f"加载网络安全日报缓存失败: {_CACHE_FILE}")
        _cache.clear()


def _save_cache() -> None:
    cutoff = (asia_shanghai_now() - timedelta(days=KEEP_DAYS)).date().isoformat()
    for key in [k for k in _cache if k < cutoff]:
        del _cache[key]
    payload = {key: value.model_dump(mode="json") for key, value in _cache.items()}
    atomic_write_text(_CACHE_FILE, json.dumps(payload, ensure_ascii=False, indent=2))


_load_cache()


async def _get_or_fetch(target: date) -> SecurityDaily | None:
    """带缓存地获取指定日期的日报，未发布时记录负缓存"""
    key = target.isoformat()
    if daily := _cache.get(key):
        return daily

    miss_ts = _miss_cache.get(key)
    if miss_ts is not None and time.monotonic() - miss_ts < MISS_CACHE_TTL:
        return None

    async with _fetch_lock:
        if daily := _cache.get(key):  # 双重检查，避免并发重复抓取
            return daily
        credential = await _get_credential()
        if credential is None:
            logger.warning(
                "未获取到 B 站登录凭证，动态接口可能不会返回数据（请配置 parser_bili_ck 或使用 blogin 扫码登录）"
            )
        daily = await _fetch_security_daily(target, credential)
        if daily is not None:
            _cache[key] = daily
            _save_cache()
        else:
            _miss_cache[key] = time.monotonic()
            logger.info(f"未找到 {target} 的网络安全日报（可能尚未发布）")
        return daily


async def get_latest_security_daily() -> SecurityDaily | None:
    """获取最新一期日报：优先当天，未发布时向前回退找最近一期"""
    today = asia_shanghai_now().date()
    for delta in range(FALLBACK_DAYS):
        if daily := await _get_or_fetch(today - timedelta(days=delta)):
            return daily
    return None


async def download_daily_images(daily: SecurityDaily) -> list[bytes]:
    """下载日报的全部图片内容"""
    images: list[bytes] = []
    for url in daily.images:
        resp = await HTTPX_CLIENT.get(url, headers=IMG_HEADERS, follow_redirects=True)
        resp.raise_for_status()
        images.append(resp.content)
    return images
