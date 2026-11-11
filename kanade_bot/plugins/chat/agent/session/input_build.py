"""轮次输入构建：提示词分段、图片暂存沙箱与多模态内容构建"""

import asyncio
import base64
import mimetypes
import uuid
from collections import deque
from io import BytesIO
from pathlib import Path

from nonebot import logger
from pydantic_ai import BinaryContent, UserContent

from kanade_bot.utils.image import compress_image
from kanade_bot.utils.parse import ImageInput, build_sender_info
from kanade_bot.utils.session import SessionInfo

from ...config import cfg
from ..sandbox import SandboxSession
from ..vision import get_image_caption


def _prompt_parts(
    session_info: SessionInfo,
    prompt: str,
    *,
    rag_docs: list[str] | None = None,
    messages: deque[str] | None = None,
    reply_text: str | None = None,
    system_notification: str | None = None,
    staged_images: list[str] | None = None,
) -> list[str]:
    """构建本轮用户输入的文本分段"""
    parts: list[str] = []

    if rag_docs:
        parts.append("检索到可能相关的文档：\n" + "\n\n".join(rag_docs))
    if messages:
        parts.append("下面是之前的消息缓冲区中的消息：\n" + "\n\n".join(messages))
    if reply_text:
        parts.append("用户引用了之前的消息：\n" + reply_text)

    if prompt:
        if user_info := build_sender_info(session_info.nickname, session_info.user_id):
            prompt = f"{user_info}：{prompt}"
        parts.append("下面是这次用户对你的消息：\n" + prompt)

    if staged_images:
        parts.append(
            "本轮用户发送的图片已保存到沙箱工作区（工具可用这些相对路径访问）：\n"
            + "\n".join(f"- {p}" for p in staged_images)
        )

    if system_notification:
        parts.append(f"<system_notification>\n{system_notification}\n</system_notification>")

    return parts


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
    session_info: SessionInfo,
    prompt: str,
    images: list[ImageInput] | None = None,
    *,
    rag_docs: list[str] | None = None,
    messages: deque[str] | None = None,
    reply_text: str | None = None,
    system_notification: str | None = None,
    staged_images: list[str] | None = None,
) -> list[UserContent]:
    """构建本轮发送给模型的完整用户输入内容"""
    contents: list[UserContent] = [
        *_prompt_parts(
            session_info,
            prompt,
            rag_docs=rag_docs,
            messages=messages,
            reply_text=reply_text,
            system_notification=system_notification,
            staged_images=staged_images,
        )
    ]

    if cfg.agent.vision:
        for image in images or []:
            # data为None表示图片内容获取失败，仅剩名称占位，无可发送内容
            if not image.data:
                continue
            raw = base64.b64decode(image.data)
            data, media_type = await asyncio.to_thread(compress_image, raw, image.mime_type)
            contents.append(BinaryContent(data=data, media_type=media_type))
        return contents

    # 模型无视觉：转述
    for image in images or []:
        # data为None表示图片内容获取失败，仅剩名称占位，无法转述
        if cfg.image_caption and image.data and image.mime_type:
            caption = await get_image_caption(image.data, image.mime_type)
            if caption:
                contents.append(f"图片 {image.name} 的文字描述：\n{caption}")
                continue
        contents.append(f"收到图片 {image.name}，但当前无法查看图片内容")
    return contents
