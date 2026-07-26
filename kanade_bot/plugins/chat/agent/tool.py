import base64

from copilot import define_tool
from copilot.session import Attachment
from copilot.session_events import AssistantMessageData
from copilot.tools import ToolBinaryResult, ToolResult
from nonebot import logger
from pydantic import BaseModel, Field

from kanade_bot.utils.common import COPILOT_CLIENT, HTTPX_CLIENT

from ..config import cfg, chat_configs


class TavilySearchParams(BaseModel):
    query: str = Field(description="Search query.")


@define_tool(
    "list_memes",
    description="""List the available meme dictionary where keys are meme names and values are descriptions.
To use a meme in a reply, reference it as {{meme_name}}, for example {{happy}};
it will be automatically replaced with the corresponding meme image when sent.""",
)
def list_memes():
    return chat_configs.instance.memes


IMAGE_CAPTION_SYSTEM_PROMPT = """你是一个图片转述模型，负责将图片内容转述为文字描述。
请充分查看、分析和理解图片内容，详细地描述图片内容中的场景、元素等信息，避免遗漏重要信息。
输出要求：直接输出图片的文字描述，不要包含任何额外的解释或说明。
"""


async def get_image_caption(attachment: Attachment) -> str | None:
    """使用图片转述模型获取图片的文字描述"""
    session = await COPILOT_CLIENT.create_session(
        model=cfg.image_caption_model,
        provider=cfg.image_caption_provider,
        system_message={
            "mode": "replace",
            "content": IMAGE_CAPTION_SYSTEM_PROMPT,
        },
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


@define_tool(
    "view_image",
    description="Image viewer tool. Provide one image URL, and"
    "if you have vision capabilities, it will return the image content;"
    "otherwise, it will return a text caption for the image.",
)
async def view_image(url: str) -> ToolResult | str:
    r = await HTTPX_CLIENT.get(url)
    logger.info(
        "调用工具{}，查看图片：{}，返回了结果，状态码：{}",
        view_image.name,
        url,
        r.status_code,
    )
    if r.status_code != 200:
        return f"无法查看图片，URL: {url}，状态码: {r.status_code}"

    data = base64.b64encode(r.content).decode()
    mine_type = r.headers.get("Content-Type", "application/octet-stream")
    if cfg.image_caption_model:
        caption = await get_image_caption(
            {
                "type": "blob",
                "data": data,
                "mimeType": mine_type,
                "displayName": url,
            }
        )
        return caption or "无法获取图片内容的文字描述。"

    image = ToolBinaryResult(
        data=data,
        mime_type=mine_type,
        type="image",
        description=url,
    )
    return ToolResult(
        text_result_for_llm="图片查看结果",
        binary_results_for_llm=[image],
    )
