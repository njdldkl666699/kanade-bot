import asyncio
import json
import os
import re
from datetime import date

import nonebot
from nonebot import require

nonebot.init()

require("nonebot_plugin_parser")
from nonebot_plugin_parser.parsers.bilibili import BilibiliParser

# parser = BilibiliParser()

# result = asyncio.run(parser.parse_opus_by_id(1271855239340752901))
# print(result)
# result.graphics

####----

from bilibili_api import Credential, user, sync, comment, opus
from bilibili_api.comment import CommentResourceType

SESSDATA = os.environ.get("SESSDATA", "")

credential = Credential(sessdata=SESSDATA)
# u = user.User(72338055)


# # 用于记录下一次起点
# offset = ""

# # 用于存储所有动态
# dynamics = []

# page = sync(u.get_dynamics_new(offset))

# print("page: ", page)

# json.dump(page, open("page.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)

# o = opus.Opus(1271855239340752901, credential=credential)
# rid = sync(o.get_rid())
# print("rid: ", rid)
# comments = sync(
#     comment.get_comments_lazy(rid, CommentResourceType.DYNAMIC_DRAW, credential=credential)
# )
# # print("comments: ", comments)
# json.dump(comments, open("comments.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
# # 无限循环，直到 has_more != 1
# while True:
#     # 获取该页动态
#     page = sync(u.get_dynamics_new(offset))
#     print("page: ", page)

#     dynamics.extend(page["items"])

#     if page["has_more"] != 1:
#         # 如果没有更多动态，跳出循环
#         break

#     # 设置 offset，用于下一轮循环
#     offset = page["offset"]

# # 打印动态数量
# print(f"共有 {len(dynamics)} 条动态")


####---- 网络安全日报：按日期定位动态 + 置顶评论 ----

WEEKDAY_ZH = "一二三四五六日"


def _daily_pattern(target: date) -> re.Pattern:
    """构造「#网络安全日报#2026年11月18日，星期三」形式的正则（月/日容忍补零写法）"""
    return re.compile(
        rf"#网络安全日报#{target.year}年0?{target.month}月0?{target.day}日"
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


async def get_security_daily(
    target_date: date | None = None,
    uid: int = 72338055,
    credential: Credential | None = None,
    max_pages: int = 10,
) -> dict | None:
    """翻页遍历 uid 的动态，返回第一条匹配「#网络安全日报#<target_date>」的动态信息。

    返回:
        {
            "dynamic_id": ...,
            "dynamic_url": "https://www.bilibili.com/opus/<dynamic_id>",
            "text": "#网络安全日报#2026年11月18日，星期三",
            "images": ["http://i0.hdslb.com/bfs/new_dyn/....png"],
            "top_comment": "https://www.yijinglab.com/industry/...",  # 置顶评论原文(top.upper)
            "top_comment_url": "https://www.yijinglab.com/industry/...",  # 评论中提取的第一个 url
        }
        未找到返回 None。
    """
    target_date = target_date or date.today()
    pattern = _daily_pattern(target_date)
    credential = credential or Credential(sessdata=SESSDATA)
    u = user.User(uid)

    offset = ""
    for _ in range(max_pages):
        page = await u.get_dynamics_new(offset)
        for item in page.get("items") or []:
            text = _dynamic_text(item)
            if not pattern.search(text):
                continue

            dynamic_id = item.get("id_str") or str(item.get("id") or "")
            basic = item.get("basic") or {}

            # 评论区 rid/type 优先取动态 basic 自带字段，省一次 opus.get_rid() 请求
            rid = basic.get("comment_id_str") or basic.get("rid_str") or ""
            if rid:
                comment_type = CommentResourceType(
                    basic.get("comment_type") or CommentResourceType.DYNAMIC_DRAW.value
                )
            else:
                rid = await opus.Opus(int(dynamic_id), credential=credential).get_rid()
                comment_type = CommentResourceType.DYNAMIC_DRAW

            comments = await comment.get_comments_lazy(
                int(rid), comment_type, credential=credential
            )
            top_upper = (comments.get("top") or {}).get("upper") or {}
            top_message = ((top_upper.get("content") or {}).get("message") or "").strip()
            url_match = re.search(r"https?://\S+", top_message)

            return {
                "dynamic_id": dynamic_id,
                "dynamic_url": f"https://www.bilibili.com/opus/{dynamic_id}",
                "text": text.strip(),
                "images": _dynamic_images(item),
                "top_comment": top_message or None,
                "top_comment_url": url_match.group(0) if url_match else None,
            }

        if str(page.get("has_more")).lower() not in ("1", "true"):
            break
        offset = page.get("offset") or ""

    return None


if __name__ == "__main__":
    # 注意：bilibili_api 的 sync() 会自建事件循环，与 asyncio.run() 在同一进程混用
    # 会导致后续请求拿到空响应，这里统一用 sync() 跑协程
    daily = sync(get_security_daily())  # 默认今天；可传 date(2026, 11, 16) 查历史
    print(json.dumps(daily, ensure_ascii=False, indent=2) if daily else "未找到匹配动态")
