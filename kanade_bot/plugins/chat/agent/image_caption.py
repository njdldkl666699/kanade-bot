from copilot import PermissionHandler
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
        logger.warning("未配置图片转述模型，无法获取图片转述")
        return

    if (p := cfg.system_prompt_file_path) and p.is_file():
        system_prompt = p.read_text(encoding="utf-8")
    else:
        system_prompt = FALLBACK_SYSTEM_PROMPT

    session = await COPILOT_CLIENT.create_session(
        on_permission_request=PermissionHandler.approve_all,
        client_name="kanade-bot-image-caption",
        model=cfg.model,
        provider=cfg.provider.model_dump(exclude_unset=True) if cfg.provider else None,  # pyright: ignore[reportArgumentType]
        reasoning_effort=cfg.reasoning_effort,
        system_message={
            "mode": "replace",
            "content": system_prompt,
        },
        available_tools=cfg.available_tools,
        excluded_tools=cfg.excluded_tools,
        mcp_servers=cfg.mcp_servers,
        large_output={"enabled": False},
    )
    try:
        async with session:
            event = await session.send_and_wait(
                prompt="请描述这张图片的内容。",
                attachments=[attachment],
                timeout=180,
            )
    except Exception as e:  # noqa: BLE001
        logger.exception("获取图片转述时发生错误: {}", e)
        return

    if not event:
        logger.warning(f"图片转述模型未返回结果，图片URL: {attachment.get('displayName')}")
        return

    match event.data:
        case AssistantMessageData() as data:
            return data.content.strip()
