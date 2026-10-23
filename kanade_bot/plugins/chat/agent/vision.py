import asyncio
import base64

from nonebot import logger
from pydantic_ai import Agent, BinaryContent, UserContent

from kanade_bot.utils.pai_runtime import build_model_settings, get_model, run_with_continuation

from ..config import cfg as chat_cfg
from ..meme_index import TAGGING_FALLBACK_PROMPT, parse_model_tags

FALLBACK_SYSTEM_PROMPT = """你是一个图片转述模型，负责将图片内容转述为文字描述。
请充分查看、分析和理解图片内容，详细地描述图片内容中的场景、元素等信息，避免遗漏重要信息。
输出要求：直接输出图片的文字描述，不要包含任何额外的解释或说明。
"""


def _build_caption_agent() -> Agent | None:
    cfg = chat_cfg.image_caption
    if cfg is None:
        return None
    system_prompt = FALLBACK_SYSTEM_PROMPT
    if (p := cfg.system_prompt_file_path) and p.is_file():
        system_prompt = p.read_text(encoding="utf-8")

    return Agent(
        name="kanade-bot-image-caption",
        instructions=system_prompt,
        model=get_model(cfg),
        model_settings=build_model_settings(cfg),
    )


async def get_image_caption(data: str, mime_type: str) -> str | None:
    """使用图片转述模型获取图片的文字描述

    :param data: 图片内容的base64字符串
    :param mime_type: 图片MIME类型
    :returns: 图片的文字描述，发生错误时返回错误信息
    """
    agent = _build_caption_agent()
    if agent is None:
        msg = "未配置图片转述模型，无法获取图片转述"
        logger.warning(msg)
        return msg

    user_prompt: list[UserContent] = [
        BinaryContent(data=base64.b64decode(data), media_type=mime_type),
        "请描述这张图片的内容。",
    ]
    try:
        content = await asyncio.wait_for(
            run_with_continuation(agent, user_prompt, max_requests=1), timeout=180
        )
    except Exception as e:
        msg = f"获取图片转述时发生错误: {e}"
        logger.exception(msg)
        return msg

    if not content.strip():
        logger.warning("图片转述模型未返回结果")
        return None
    return content.strip()


def _build_tagging_agent() -> Agent | None:
    cfg = chat_cfg.sticker_meme.tagger
    if cfg is None:
        return None
    # 打标提示词：配置文件优先，缺失时回退内置默认
    system_prompt = TAGGING_FALLBACK_PROMPT
    if (p := cfg.system_prompt_file_path).is_file():
        system_prompt = p.read_text(encoding="utf-8").strip() or TAGGING_FALLBACK_PROMPT

    return Agent(
        name="kanade-bot-sticker-tagging",
        instructions=system_prompt,
        model=get_model(cfg),
        model_settings=build_model_settings(cfg),
    )


async def get_image_tags(data: bytes, mime_type: str) -> list[str] | None:
    """用视觉模型为表情包生成语义检索标签

    模型与提示词由 `chat.sticker_meme` 配置，与图片转述相互独立。
    输出解析失败时重试一次；模型未配置或始终失败时返回 None。

    :param data: 图片二进制内容
    :param mime_type: 图片 MIME 类型
    """
    agent = _build_tagging_agent()
    if agent is None:
        logger.warning("未配置表情包打标模型（chat.sticker_meme.tagger），无法自动打标")
        return None

    user_prompt: list[UserContent] = [
        BinaryContent(data=data, media_type=mime_type),
        "请为这张表情包生成标签。",
    ]
    for attempt in range(2):
        try:
            content = await asyncio.wait_for(
                run_with_continuation(agent, user_prompt, max_requests=1), timeout=180
            )
        except Exception:
            logger.exception("表情包打标请求失败（第 %s 次尝试）", attempt + 1)
            continue
        if tags := parse_model_tags(content):
            return tags
        logger.warning(
            "表情包打标输出无法解析（第 %s 次），原始输出：%s", attempt + 1, content[:200]
        )
    return None
