"""冒烟：回复Markdown图片语法解析（块级/行内） / download_file文件名推断 / 工具注册表"""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot import init_nonebot, register_adapters_and_load_plugins
from scripts.util import load_configs

# 复用生产初始化路径（不 run 不监听；model_updater 导入期需要 ASGI driver）
_env, _configs = load_configs(Path(__file__).parents[2])
init_nonebot(env=_env, **_configs)
register_adapters_and_load_plugins()


import base64

from kanade_bot.plugins.chat.agent.tools import build_tools
from kanade_bot.plugins.chat.agent.tools.media import view_image
from kanade_bot.plugins.chat.agent.tools.messaging import _download_file_name
from kanade_bot.plugins.chat.deliver import MARKDOWN_IMAGE_RE, parse_reply_messages

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6260010000000500010d0a2db40000000049454e44ae426082"
)


async def main():
    with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as od:
        root = Path(td)
        (root / "rendered").mkdir()
        (root / "rendered" / "a.png").write_bytes(PNG_1PX)
        outside = Path(od) / "outside.png"
        outside.write_bytes(PNG_1PX)

        # 1) 行内图片：与前后文本按原始顺序合并为同一条消息
        msgs = await parse_reply_messages("看这张 ![猫](https://example.com/cat.jpg) 好可爱", root)
        assert len(msgs) == 1, msgs
        assert [s.type for s in msgs[0]] == ["text", "image", "text"], msgs[0]
        assert msgs[0][1].data["file"] == "https://example.com/cat.jpg"
        assert msgs[0][0].data["text"] == "看这张" and msgs[0][2].data["text"] == "好可爱"

        # 2) 块级图片（空行分隔）：文本与图片各自成一条消息
        msgs = await parse_reply_messages("画好了：\n\n![图](rendered/a.png)", root)
        assert len(msgs) == 2, msgs
        assert [s.type for s in msgs[0]] == ["text"] and msgs[0][0].data["text"] == "画好了："
        assert [s.type for s in msgs[1]] == ["image"], msgs[1]
        assert msgs[1][0].data["file"] == "base64://" + base64.b64encode(PNG_1PX).decode()

        # 2b) 块级图片（独立成行，单换行）：前后文本各成一条消息，图片单独发送
        msgs = await parse_reply_messages("画好了：\n![图](rendered/a.png)\n喜欢吗", root)
        assert len(msgs) == 3, msgs
        assert [s.type for s in msgs[0]] == ["text"] and msgs[0][0].data["text"] == "画好了："
        assert [s.type for s in msgs[1]] == ["image"]
        assert [s.type for s in msgs[2]] == ["text"] and msgs[2][0].data["text"] == "喜欢吗"

        # 2c) 行内本地图片与文本合并进同一条消息
        msgs = await parse_reply_messages("看这张 ![图](rendered/a.png) 怎么样", root)
        assert len(msgs) == 1 and [s.type for s in msgs[0]] == ["text", "image", "text"], msgs

        # 3) 绝对路径：工作区内允许，工作区外拒绝，未提供工作区根也拒绝
        msgs = await parse_reply_messages(f"![x]({root / 'rendered' / 'a.png'})", root)
        assert len(msgs) == 1 and [s.type for s in msgs[0]] == ["image"], msgs
        msgs = await parse_reply_messages(f"![x]({outside})", root)
        assert len(msgs) == 1 and "图片失效" in msgs[0][0].data["text"], msgs
        msgs = await parse_reply_messages(f"![x]({root / 'rendered' / 'a.png'})", None)
        assert len(msgs) == 1 and "图片失效" in msgs[0][0].data["text"], msgs

        # 4) 沙箱式绝对路径（/rendered/... 以工作区根再试）与越界相对路径
        msgs = await parse_reply_messages("![x](/rendered/a.png)", root)
        assert len(msgs) == 1 and [s.type for s in msgs[0]] == ["image"], msgs
        msgs = await parse_reply_messages("![x](../../etc/passwd.png)", root)
        assert len(msgs) == 1 and "图片失效" in msgs[0][0].data["text"], msgs

        # 5) 本地路径但未提供工作区根 / 文件不存在 → 失效提示文本
        msgs = await parse_reply_messages("![x](rendered/a.png)", None)
        assert len(msgs) == 1 and "图片失效" in msgs[0][0].data["text"], msgs
        msgs = await parse_reply_messages("![x](rendered/none.png)", root)
        assert len(msgs) == 1 and "图片失效" in msgs[0][0].data["text"], msgs

        # 5b) 块级失效图降级为文字段，随前后行文本合并在同一条消息
        msgs = await parse_reply_messages("A\n![x](rendered/none.png)\nB", root)
        assert len(msgs) == 1 and [s.type for s in msgs[0]] == ["text", "text", "text"], msgs
        assert [s.data["text"] for s in msgs[0]] == [
            "A",
            "[图片失效：rendered/none.png]",
            "B",
        ]

        # 6) 代码块内图片语法不解析
        content = "```markdown\n![x](rendered/a.png)\n```\n\n正文"
        msgs = await parse_reply_messages(content, root)
        assert [len(m) for m in msgs] == [1, 1] and all(
            s.type == "text" for m in msgs for s in m
        ), msgs
        assert "![x](rendered/a.png)" in msgs[0][0].data["text"]

        # 6b) @按原始位置替换为at消息段，与文本合并在同一条消息
        msgs = await parse_reply_messages("你好 @12345678 看图", root)
        assert len(msgs) == 1 and [s.type for s in msgs[0]] == ["text", "at", "text"], msgs
        assert msgs[0][1].data["qq"] == "12345678"

        # 7) 正则：带标题形式 / 裸链接形式
        m = MARKDOWN_IMAGE_RE.search('![a](u.png "标题")')
        assert m and m.group(2) == "u.png"
        assert MARKDOWN_IMAGE_RE.search("![](https://x.y/z?a=1)").group(2) == "https://x.y/z?a=1"
        assert MARKDOWN_IMAGE_RE.search("普通文本 [链接](x.md)") is None

    # 8) 下载文件名推断：显式 > URL推断 > file_id；防路径穿越
    assert _download_file_name("报告.pdf", "https://x/y", "fid1") == "报告.pdf"
    assert _download_file_name("", "https://x/y/%E6%8A%A5%E5%91%8A.pdf", "fid1") == "报告.pdf"
    assert _download_file_name("", "https://x/y/", "fid1") == "fid1"
    assert _download_file_name("../../evil.sh", "https://x/y", "fid1") == "evil.sh"

    # 9) 工具注册表：send_image 已移除，download_file 已注册，view_image有original参数
    tools = build_tools()
    names = {t.__name__ for t in tools}
    assert "send_image" not in names, names
    assert "download_file" in names, names
    sig = inspect.signature(view_image)
    assert sig.parameters["original"].default is False

    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
