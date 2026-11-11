"""冒烟：build_user_content 分段式多模态输入组装（含私有 _prompt_parts 分段逻辑）"""

from __future__ import annotations

import asyncio
import base64
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from bot import init_nonebot, register_adapters_and_load_plugins
from scripts.util import load_configs

# 复用生产初始化路径（不 run 不监听；model_updater 导入期需要 ASGI driver）
_env, _configs = load_configs(Path(__file__).parents[2])
init_nonebot(env=_env, **_configs)
register_adapters_and_load_plugins()

from pydantic_ai import BinaryContent

from kanade_bot.plugins.chat.agent.session.input_build import (
    _prompt_parts,
    build_user_content,
)
from kanade_bot.utils.parse import ImageInput
from kanade_bot.utils.session import SessionInfo

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6260010000000500010d0a2db40000000049454e44ae426082"
)

INFO = SessionInfo(session_id="qq-group-1", nickname="小明", user_id="10001", group_id="1")


async def main():
    # 1) 全量分段：各来源独立成段，无 `$` 前缀
    parts = _prompt_parts(
        INFO,
        "你好",
        rag_docs=["文档A", "文档B"],
        messages=deque(["[00:01] 小明：早", "[00:02] 小红：早呀"]),
        reply_text="[00:00] 小明：昨天的图",
        system_notification="定时任务触发",
        staged_images=["images/{ABC}.jpg"],
    )
    assert len(parts) == 6, parts
    assert parts[0] == "检索到可能相关的文档：\n\n文档A\n\n文档B"
    assert parts[1] == "下面是之前的消息缓冲区中的消息：\n\n[00:01] 小明：早\n\n[00:02] 小红：早呀"
    assert parts[2] == "用户引用了之前的消息：\n[00:00] 小明：昨天的图"
    assert parts[3] == "下面是这次用户对你的消息：\n\n小明[id=10001]：你好"
    assert (
        parts[4].startswith("本轮用户发送的图片已保存到沙箱工作区")
        and "- images/{ABC}.jpg" in parts[4]
    )
    assert parts[5] == "<system_notification>\n定时任务触发\n</system_notification>"
    assert not any("$" in p for p in parts)

    # 2) 仅prompt：单段
    parts = _prompt_parts(INFO, "在吗")
    assert parts == ["下面是这次用户对你的消息：\n\n小明[id=10001]：在吗"], parts
    # 空prompt空上下文：空分段列表（运行时有前置守卫，不会以空输入发轮次）
    assert _prompt_parts(INFO, "") == []

    # 3) 无昵称时不加发送者前缀
    parts = _prompt_parts(SessionInfo(session_id="s"), "hi")
    assert parts == ["下面是这次用户对你的消息：\n\nhi"], parts

    # 4) 视觉模型：文本分段 + 压缩后的图片二进制部分（合并后的单一入口）
    from kanade_bot.plugins.chat.config import cfg

    img = ImageInput(name="a.png", data=base64.b64encode(PNG_1PX).decode(), mime_type="image/png")
    content = await build_user_content(INFO, "看图", [img])
    if cfg.agent.vision:
        assert content[:1] == ["下面是这次用户对你的消息：\n\n小明[id=10001]：看图"] and isinstance(
            content[1], BinaryContent
        ), content
        assert content[1].is_image
    else:
        # 无视觉：带data的图片本应转述；此处仅验证分段原样保留
        assert content[:1] == ["下面是这次用户对你的消息：\n\n小明[id=10001]：看图"], content

    # 5) 图片内容缺失（data=None）：视觉分支跳过，无视觉分支降级为占位描述段
    placeholder = ImageInput(name="b.jpg", data=None, mime_type=None)
    content = await build_user_content(INFO, "在吗", [placeholder])
    prompt_part = "下面是这次用户对你的消息：\n\n小明[id=10001]：在吗"
    if cfg.agent.vision:
        assert content == [prompt_part], content
    else:
        assert content == [prompt_part, "收到图片 b.jpg，但当前无法查看图片内容"], content

    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
