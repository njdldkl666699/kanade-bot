"""轮次输入构建：提示词组装、图片暂存沙箱与多模态内容构建"""

import base64
import mimetypes
import uuid
from collections import deque
from io import BytesIO
from pathlib import Path

from nonebot import logger
from pydantic_ai import BinaryContent, UserContent

from kanade_bot.utils.parse import ImageInput, build_sender_info
from kanade_bot.utils.session import SessionInfo

from ...config import cfg
from ..sandbox import SandboxSession
from ..vision import get_image_caption


def build_send_prompt(
    session_info: SessionInfo,
    prompt: str,
    *,
    rag_docs: list[str] | None = None,
    messages: deque[str] | None = None,
    reply_text: str | None = None,
    system_notification: str | None = None,
) -> str:
    """构建发送给模型的完整提示词"""
    prompt_parts: list[str] = []

    if rag_docs:
        prompt_parts.append("$ 检索到可能相关的文档：")
        prompt_parts.extend(rag_docs)
    if messages:
        prompt_parts.append("$ 下面是之前的消息缓冲区中的消息：")
        prompt_parts.extend(messages)
    if reply_text:
        prompt_parts.append("$ 用户引用了之前的消息：")
        prompt_parts.append(reply_text)

    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        prompt = f"{user_info}：{prompt}"
    if prompt:
        prompt_parts.append("$ 下面是这次用户对你的消息：")
        prompt_parts.append(prompt)

    if system_notification:
        prompt_parts.append(f"<system_notification>\n{system_notification}\n</system_notification>")

    return "\n\n".join(prompt_parts).strip()


async def stage_images_to_sandbox(
    sandbox: SandboxSession, images: list[ImageInput] | None
) -> list[str]:
    """把本轮用户图片写入沙箱工作区 images/ 目录，返回相对路径列表"""
    staged: list[str] = []
    for image in images or []:
        if not image.data:
            continue
        data = base64.b64decode(image.data)
        name = Path(image.name).name or "image"
        if not Path(name).suffix and image.mime_type:
            name += mimetypes.guess_extension(image.mime_type) or ".jpg"
        target = Path("images") / name
        try:
            existing = (await sandbox.read(target)).read()
        except FileNotFoundError:
            existing = None
        if existing == data:
            staged.append(str(target))  # 同一张图重发，直接复用
            continue
        if existing is not None:
            target = Path("images") / f"{uuid.uuid4().hex[:8]}_{name}"
        try:
            await sandbox.write(target, BytesIO(data))
        except Exception as e:
            logger.warning(f"暂存图片到沙箱工作区失败: {image.name}: {e}")
            continue
        staged.append(str(target))
    return staged


async def build_user_content(
    send_prompt: str, images: list[ImageInput] | None
) -> list[UserContent]:
    """构建本轮用户输入：文本 + 图片"""
    content: list[UserContent] = []

    if cfg.agent.vision:
        content.append(send_prompt)
        for image in images or []:
            content.append(
                BinaryContent(
                    data=base64.b64decode(image.data or ""),
                    media_type=image.mime_type or "image/jpeg",
                )
            )
        return content

    # 模型无视觉：转述
    parts = [send_prompt]

    for image in images or []:
        # data为None表示图片内容获取失败，仅剩名称占位，无法转述
        if cfg.image_caption and image.data and image.mime_type:
            caption = await get_image_caption(image.data, image.mime_type)
            if caption:
                parts.append(f"\n$ 图片 {image.name} 的文字描述: \n{caption}")
                continue
        parts.append(f"\n$ 收到图片 {image.name}，但当前无法查看图片内容")
    content.append("\n".join(parts))
    return content
