"""冒烟：format_forward_message_for_ai / get_forward_message 标记容错

只验证 content 分支（不发真实 API 请求）与工具侧纯逻辑。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from bot import init_nonebot, register_adapters_and_load_plugins
from scripts.util import load_configs

# 复用生产初始化路径（不 run 不监听；model_updater 导入期需要 ASGI driver）
_env, _configs = load_configs(Path("."))
init_nonebot(env=_env, **_configs)
register_adapters_and_load_plugins()

from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageSegment

from kanade_bot.utils.parse import format_forward_message_for_ai


def make_event(user_id: int, nickname: str, text: str, image: str | None = None):
    segs = [MessageSegment.text(text)]
    if image:
        segs.append(MessageSegment.image(image))
    return {
        "post_type": "message",
        "message_type": "group",
        "sub_type": "normal",
        "message_id": 1,
        "user_id": user_id,
        "message": [{"type": "text", "data": {"text": text}}]
        + ([{"type": "image", "data": {"file": image}}] if image else []),
        "raw_message": text,
        "font": 0,
        "sender": {
            "user_id": user_id,
            "nickname": nickname,
            "card": "",
            "sex": "unknown",
            "age": 0,
        },
        "time": 0,
        "self_id": 10000,
        "group_id": 100,
    }


class FakeBot(OneBot):
    def __init__(self):  # type: ignore[reportMissingSuperCall]
        pass

    async def get_group_info(self, group_id: int, no_cache: bool = False):
        return {"group_id": group_id, "group_name": "测试群"}


async def main():
    bot = FakeBot()
    content = [make_event(1, "Alice", "看这张图", "{ABC123}.jpg"), make_event(2, "Bob", "好可爱")]
    seg = MessageSegment("forward", {"id": "fwd123", "content": content})

    # 1) fetch_images=True（与旧内联展开行为一致；FakeBot拉不到图 → 无附件、留占位）
    text, atts = await format_forward_message_for_ai(bot, seg)
    print("=== fetch_images=True ===")
    print(text)
    assert text.startswith("<forward id=fwd123>") and text.endswith("</forward>")
    assert "Alice[id=1]：看这张图\n[图片：{ABC123}.jpg]" in text
    assert "Bob[id=2]：好可爱" in text
    assert atts == []  # FakeBot 无法调用 get_image，图片拉取失败仅留占位
    print("附件:", [(a.name, a.data is None) for a in atts])

    # 2) fetch_images=False（工具路径：图片只留占位，不拉字节）
    text2, atts2 = await format_forward_message_for_ai(bot, seg, fetch_images=False)
    print("=== fetch_images=False ===")
    print(text2)
    assert "[图片：{ABC123}.jpg]" in text2
    assert atts2 == []
    # 两模式下文本一致
    assert text == text2

    # 3) 工具侧标记容错与空内容检测
    from kanade_bot.plugins.chat.agent.tools.messaging import _FORWARD_MARKER_RE

    m = _FORWARD_MARKER_RE.fullmatch("[forward:id=fwd123]".strip())
    assert m and m.group(1) == "fwd123"
    m2 = _FORWARD_MARKER_RE.fullmatch("[forward:id=abc def]")
    assert m2 is None  # 含空格的id不匹配fullmatch，原样传递
    assert _FORWARD_MARKER_RE.fullmatch("fwd123") is None  # 裸id不误匹配

    # 空事件 → 工具的空内容判定串
    empty_seg = MessageSegment("forward", {"id": "fwdX", "content": []})
    text3, _ = await format_forward_message_for_ai(bot, empty_seg, fetch_images=False)
    assert text3 == "<forward id=fwdX>\n</forward>", repr(text3)

    print("ALL OK")


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
