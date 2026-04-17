import random
import re
from base64 import b64encode
from pathlib import Path

from copilot.session import Attachment
from nonebot import logger
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher

from ..util import OneBotMessageSegmentMeme, resolve_session_id_and_prompt
from .ban import is_banned
from .client import file_client as client
from .config import PlatformType, cfg, configs
from .copilot import copilot
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

    size = copilot.get_session_prompt_buffer_size(session_id)
    threshold = auto_reply_config.threshold
    # 阈值小于等于0，或当前消息数小于阈值，不触发自动回复
    if threshold <= 0 or size < threshold:
        return False

    # 达到阈值，按照概率决定是否自动回复
    # 生成一个0.0到1.0之间的随机数，如果小于配置的概率，则触发自动回复
    return random.random() < auto_reply_config.probability


async def resolve_message_images(message: OneBotMessage) -> list[Attachment]:
    """解析消息中的图片并返回附件列表"""
    attachments: list[Attachment] = []
    for segment in message:
        if segment.type != "image":
            continue

        displayName: str = segment.data["file"] or "image.png"
        url: str | None = segment.data["url"]
        if not url:
            continue

        response = await client.get(url)
        response.raise_for_status()
        data = b64encode(response.content).decode()
        attachments.append(
            {
                "type": "blob",
                "data": data,
                "mimeType": "image/png",
                "displayName": displayName,
            }
        )
    return attachments


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
    chunks: list[str],
    *,
    reply_id: int | None = None,
):
    messages: list[OneBotMessage] = []

    def replace_meme(match: re.Match[str]) -> str:
        meme_name = match.group(1)
        if meme_name not in configs.memes:
            return ""

        meme_path = Path(cfg.chat_memes_path) / meme_name
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
    node_custom_message = OneBotMessage()
    for message in messages:
        node_custom_message += MessageSegment.node_custom(cfg.bot_id, cfg.bot_nickname, message)
    await matcher.finish(node_custom_message)


async def send_message_in_chunks(
    matcher: type[Matcher],
    event: Event,
    prompt: str,
):
    # 处理消息中的图片附件
    attachments: list[Attachment] = []
    # 1. 回复的消息中的图片
    if isinstance(event, OneBotMessageEvent) and event.reply:
        reply_message_attachments = await resolve_message_images(event.reply.message)
        attachments.extend(reply_message_attachments)
    # 2. 发送的消息中的图片
    message = event.get_message()
    if isinstance(message, OneBotMessage):
        message_attachments = await resolve_message_images(message)
        attachments.extend(message_attachments)

    # 处理引用（回复）消息中的文本内容
    reply_text: str | None = None
    if isinstance(event, OneBotMessageEvent) and event.reply:
        reply_text = event.reply.message.extract_plain_text().strip()

    # 进行RAG查询，获取相关文档
    rag_docs = query(prompt) if prompt else None
    # 然后再将prompt带上用户昵称
    session_id, nickname, is_group = resolve_session_id_and_prompt(event, prompt)
    if nickname:
        prompt = f"{nickname} 说：{prompt}"

    response, new_session = await copilot.send_and_wait(
        session_id,
        prompt,
        is_group=is_group,
        rag_docs=rag_docs,
        reply_text=reply_text,
        attachments=attachments,
        timeout=300,
    )
    if new_session:
        logger.info(f"会话{session_id}是新会话，旧会话可能被手动删除或损坏")

    if not response or not response.data.content:
        logger.warning(f"会话{session_id}没有收到回复，可能是生成失败或超时")
        await send_fail_message(matcher)
        return

    content = response.data.content
    if isinstance(content, dict):
        logger.warning(f"生成的内容不是字符串，无法发送，内容：{content}")
        await send_fail_message(matcher)
        return

    # OneBot消息特殊处理
    if isinstance(event, OneBotMessageEvent):
        chunks = split_content_preserving_code_blocks(content)
        await finish_onebot_message(matcher, chunks, reply_id=event.message_id)

    # Console消息直接发送原始内容
    await matcher.finish(content)


def send_fail_message(matcher: type[Matcher]):
    image = Path(cfg.chat_fail_image_path)
    if image.is_file():
        return matcher.finish(OneBotMessageSegmentMeme(image))
    return matcher.finish("已深度思考（用时0秒）\n服务器繁忙，请稍后再试")
