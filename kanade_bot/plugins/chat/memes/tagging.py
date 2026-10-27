"""表情包视觉打标：标签生成 agent 与模型输出解析"""

import asyncio
import json
import re

from nonebot import logger
from pydantic_ai import Agent, BinaryContent, UserContent

from kanade_bot.utils.pai_runtime import build_model_settings, get_model, run_with_continuation

from ..config import cfg
from .index import dedupe_tags, parse_tags

TAGGING_FALLBACK_PROMPT = """你是表情包标注助手。请查看这张表情包图片，为它生成用于语义检索的中文标签。

标注要求：
1. 输出一个 JSON 数组，包含 5~10 个简短的中文标签
2. 覆盖以下维度（按图适用性取舍）：
   - 画面主体：角色/人物/生物，如"猫猫"、"小女孩"、"熊猫头"
   - 情绪状态：如"开心"、"无语"、"暴怒"、"委屈"
   - 动作神态：如"手舞足蹈"、"探头偷看"、"瘫倒"
   - 图内文字的语义：表达图内文字的含义或梗，而非抄录原文
   - 适用场景：什么话题下会想发这张图，如"敷衍回复"、"阴阳怪气"、"庆祝"
3. 标签是检索关键词：想象群友想发这张图时会怎么描述它，用大家会用的口语化说法
4. 每个标签 2~6 个字
5. 只输出 JSON 数组本身，不要输出任何其他内容

输出示例：
["猫猫", "开心", "手舞足蹈", "庆祝", "得意", "偷着乐"]
"""

_JSON_ARRAY = re.compile(r"\[[^\[\]]*\]")


def parse_model_tags(raw: str) -> list[str]:
    """宽容解析打标模型的输出：提取首个 JSON 字符串数组；失败时按分隔符切分"""
    text = str(raw or "").strip()
    if match := _JSON_ARRAY.search(text):
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, list):
                return dedupe_tags([str(x) for x in obj if str(x).strip()])
        except json.JSONDecodeError:
            pass
    return parse_tags(text)


def _build_tagging_agent() -> Agent | None:
    tagger_cfg = cfg.sticker_meme.tagger
    if tagger_cfg is None:
        return None
    # 打标提示词：配置文件优先，缺失时回退内置默认
    system_prompt = TAGGING_FALLBACK_PROMPT
    if (p := tagger_cfg.system_prompt_file_path).is_file():
        system_prompt = p.read_text(encoding="utf-8").strip() or TAGGING_FALLBACK_PROMPT

    return Agent(
        name="kanade-bot-sticker-tagging",
        instructions=system_prompt,
        model=get_model(tagger_cfg),
        model_settings=build_model_settings(tagger_cfg),
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
