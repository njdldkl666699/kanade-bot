import uuid

from copilot.session import Attachment
from copilot.session_events import AssistantMessageData
from nonebot import logger

from kanade_bot.utils.common import COPILOT_CLIENT

from ..config import cfg as chat_cfg

cfg = chat_cfg.image_caption


FALLBACK_SYSTEM_PROMPT = """你是一个图片转述模型，负责将图片内容转述为文字描述。
请充分查看、分析和理解图片内容，详细地描述图片内容中的场景、元素等信息，避免遗漏重要信息。
输出要求：直接输出图片的文字描述，不要包含任何额外的解释或说明。
"""


async def get_image_caption(attachment: Attachment) -> str | None:
    """使用图片转述模型获取图片的文字描述"""
    if not cfg:
        msg = "未配置图片转述模型，无法获取图片转述"
        logger.warning(msg)
        return msg

    if (p := cfg.system_prompt_file_path) and p.is_file():
        system_prompt = p.read_text(encoding="utf-8")
    else:
        system_prompt = FALLBACK_SYSTEM_PROMPT

    session = await COPILOT_CLIENT.create_session(
        session_id=f"image-caption-{uuid.uuid4()}",
        client_name="kanade-bot-image-caption",
        system_message={
            "mode": "replace",
            "content": system_prompt,
        },
        **cfg.model_dump_session_config(),
    )
    try:
        async with session:
            event = await session.send_and_wait(
                prompt="请描述这张图片的内容。",
                attachments=[attachment],
                timeout=180,
            )
    except Exception as e:  # noqa: BLE001
        msg = f"获取图片转述时发生错误: {e}"
        logger.exception(msg)
        return msg

    if not event:
        msg = f"图片转述模型未返回结果，图片URL: {attachment.get('displayName')}"
        logger.warning(msg)
        return msg

    match event.data:
        case AssistantMessageData() as data:
            return data.content.strip()
