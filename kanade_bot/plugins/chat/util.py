import random
import re
from pathlib import Path
from typing import cast

from copilot.generated.session_events import AssistantMessageData
from nonebot import logger
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher

from kanade_bot.utils.common import PlatformType
from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme, get_onebot_info
from kanade_bot.utils.parse import parse_message_for_ai, parse_onebot_message_for_ai
from kanade_bot.utils.session import extract_session_info

from .ban import is_banned
from .client import file_client as client
from .config import cfg, configs
from .copilot import COPILOT
from .rag import query


def should_auto_reply(group_id: str, platform: PlatformType, session_id: str):
    if is_banned(group_id, "group", platform):
        return False

    if platform == "console":
        group_config = configs.console.auto_reply_group_config
    elif platform == "onebot":
        group_config = configs.onebot.auto_reply_group_config

    # 无配置项，默认不自动回复
    if group_id not in group_config:
        return False
    auto_reply_config = group_config[group_id]

    size = COPILOT.get_session_prompt_buffer_size(session_id)
    threshold = auto_reply_config.threshold
    # 阈值小于等于0，或当前消息数小于阈值，不触发自动回复
    if threshold <= 0 or size < threshold:
        return False

    # 达到阈值，按照概率决定是否自动回复
    # 生成一个0.0到1.0之间的随机数，如果小于配置的概率，则触发自动回复
    return random.random() < auto_reply_config.probability


def split_content_preserving_code_blocks(content: str) -> list[str]:
    # 用于存储最终的块
    chunks = []

    # 找到所有代码块的位置，将它们替换为占位符
    code_blocks = []

    # 匹配 ```...``` 代码块（支持带语言标识）
    def replace_code_block(match):
        code_blocks.append(match.group(0))
        # 返回一个唯一占位符
        return f"__CODE_BLOCK_{len(code_blocks) - 1}__"

    # 先保护代码块，将代码块替换为占位符
    content_with_placeholders = re.sub(r"```[\s\S]*?```", replace_code_block, content)

    # 按两个及以上换行拆分（代码块已被保护）
    temp_chunks = [
        chunk for chunk in re.split(r"(?:\r?\n){2,}", content_with_placeholders) if chunk.strip()
    ]

    # 恢复每个块中的代码块
    for chunk in temp_chunks:
        restored_chunk = chunk
        # 替换回代码块（使用正则确保只替换占位符）
        for i, code_block in enumerate(code_blocks):
            restored_chunk = restored_chunk.replace(f"__CODE_BLOCK_{i}__", code_block)
        chunks.append(restored_chunk)

    return chunks


async def finish_onebot_message(
    matcher: type[Matcher],
    bot: OneBot,
    chunks: list[str],
    *,
    reply_id: int | None = None,
):
    messages: list[OneBotMessage] = []

    def replace_meme(match: re.Match[str]) -> str:
        meme_name = match.group(1)
        if meme_name not in configs.memes:
            return ""

        meme_path = Path(cfg.chat_memes_dir_path) / meme_name
        if not meme_path.is_dir():
            return ""

        # 获取表情包目录下的所有图片文件
        image_files = list(meme_path.glob("*"))
        if not image_files:
            return ""

        # 随机选择一张图片
        selected_image = random.choice(image_files)
        # 将动画表情添加到消息中
        meme_segment = OneBotMessageSegmentMeme(selected_image)
        messages.append(OneBotMessage(meme_segment))
        return ""  # 替换为一个空字符串，表情包会在发送时添加

    # 处理表情包引用，格式{{表情包名称}}
    for chunk in chunks:
        text = re.sub(r"\{\{(\w+?)\}\}", replace_meme, chunk)
        messages.append(OneBotMessage(text))

    if not messages:
        await matcher.finish()

    # 消息数==1，引用回复
    if len(messages) == 1:
        if reply_id:
            messages[0].insert(0, MessageSegment.reply(reply_id))
        await matcher.finish(messages[0])

    # 消息数<=3，按条发送
    if len(messages) <= 3:
        for message in messages:
            await matcher.send(message)
        await matcher.finish()

    # 消息数>3，合并转发
    bot_id, bot_nickname = await get_onebot_info(bot)
    node_custom_message = OneBotMessage()
    for message in messages:
        node_custom_message += MessageSegment.node_custom(bot_id, bot_nickname, message)
    await matcher.finish(node_custom_message)


async def send_message_in_chunks(
    matcher: type[Matcher],
    bot: Bot,
    event: Event,
):
    message = event.get_message()
    onebot = bot if isinstance(bot, OneBot) else None
    prompt, attachments = await parse_message_for_ai(message, client, onebot)

    # 处理引用（回复）消息
    reply_text: str | None = None
    if isinstance(event, OneBotMessageEvent) and event.reply:
        reply = event.reply.message
        reply_text, reply_attachments = await parse_onebot_message_for_ai(reply, client)
        attachments.extend(reply_attachments)

    # 进行RAG查询，获取相关文档
    query_str = message.extract_plain_text().strip()
    rag_docs = query(query_str) if query_str else None

    session_info = await extract_session_info(event, bot)
    session_id = session_info.session_id

    response, new_session = await COPILOT.send_and_wait(
        session_info,
        prompt,
        rag_docs=rag_docs,
        reply_text=reply_text,
        attachments=attachments,
        timeout=300,
    )
    if new_session:
        logger.info(f"会话{session_id}是新会话，旧会话可能被手动删除或损坏")

    if not response:
        logger.warning(f"会话{session_id}没有收到回复，可能是生成失败或超时")
        await send_fail_message(matcher)
        return
    if not isinstance(response.data, AssistantMessageData):
        logger.warning(
            f"会话{session_id}回复的数据不是AssistantMessageData，可能是生成失败，数据：{response.data}"
        )
        await send_fail_message(matcher)
        return

    content = response.data.content

    # OneBot消息特殊处理
    if isinstance(event, OneBotMessageEvent):
        chunks = split_content_preserving_code_blocks(content)
        await finish_onebot_message(matcher, cast(OneBot, bot), chunks, reply_id=event.message_id)

    # Console消息直接发送原始内容
    await matcher.finish(content)


def send_fail_message(matcher: type[Matcher]):
    image = Path(cfg.chat_fail_image_file_path)
    if image.is_file():
        return matcher.finish(OneBotMessageSegmentMeme(image))
    return matcher.finish("已深度思考（用时0秒）\n服务器繁忙，请稍后再试")
