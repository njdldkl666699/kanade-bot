import random
import re
from pathlib import Path
from typing import cast

from nonebot import logger, require
from nonebot.adapters import Bot, Event
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher

from kanade_bot.utils.common import MAGIKA, PlatformType, get_platform_type
from kanade_bot.utils.onebot11 import (
    OneBotMessageSegmentMeme,
    ensure_send_forward_message,
    get_bot_info,
)
from kanade_bot.utils.parse import parse_message_for_ai, parse_onebot_message_for_ai
from kanade_bot.utils.session import extract_session_info

from .agent.copilot import copilot
from .ban import is_banned
from .config import cfg, chat_configs

require("crystal")
from kanade_bot.plugins.crystal import HandlerKeyEnum, succeed_consume

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import md_to_pic

if cfg.rag.enabled:
    from .rag import query
else:
    query = lambda _: None


def _send_fail_message(matcher: type[Matcher]):
    image = Path(cfg.fail_image_file_path)
    if image.is_file():
        return matcher.finish(OneBotMessageSegmentMeme(image))
    return matcher.finish("已深度思考（用时0秒）\n服务器繁忙，请稍后再试")


async def _send_onebot_message(
    matcher: type[Matcher],
    bot: OneBot,
    event: OneBotMessageEvent,
    segments: list[MessageSegment],
    *,
    content_long: bool = False,
):
    # 根据消息段的数量决定发送方式
    if not segments:
        return

    # 消息数==1，引用回复
    if len(segments) == 1:
        reply = MessageSegment.reply(event.message_id)
        await matcher.send(reply + segments[0])

    # 消息数<=5，按条发送
    elif len(segments) <= 5:
        for segment in segments:
            await matcher.send(segment)

    # 消息数>5但<=10，合并转发
    elif len(segments) <= 10:
        info = await get_bot_info(bot)
        node_custom_message = OneBotMessage()
        for segment in segments:
            node_custom_message += MessageSegment.node_custom(*info, OneBotMessage(segment))
        await ensure_send_forward_message(matcher, bot, event, node_custom_message)

    # 消息数>10，合并相邻的文本消息段
    else:
        messages: list[OneBotMessage | str] = []
        sentinel: str = ""
        for segment in segments:
            if segment.type == "text":
                sentinel += segment.data["text"] + "\n\n"
            else:
                if sentinel := sentinel.strip():
                    messages.append(sentinel)
                    sentinel = ""
                messages.append(OneBotMessage(segment))
        if sentinel := sentinel.strip():
            messages.append(sentinel)

        if len(messages) == 1 and isinstance(m := messages[0], str):
            r = MAGIKA.identify_bytes(m.encode())
            if r.output.label == "markdown":
                # Markdown文本，渲染为图片
                image = MessageSegment.image(await md_to_pic(m))
                reply = MessageSegment.reply(event.message_id)
                await matcher.send(reply + image)
                return

        # 内容不长，直接发送消息列表
        if not content_long:
            for message in messages:
                await matcher.send(message)
            return

        # 内容长，作为合并转发消息发送
        node_custom_message = OneBotMessage()
        info = await get_bot_info(bot)
        for message in messages:
            node_custom_message += MessageSegment.node_custom(*info, message)
        await ensure_send_forward_message(matcher, bot, event, node_custom_message)


def _extract_segments_preserving_code(content: str) -> list[MessageSegment]:
    # 用于存储最终的块
    segments: list[MessageSegment] = []

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

    for chunk in temp_chunks:
        # 替换回代码块（使用正则确保只替换占位符）
        for i, code_block in enumerate(code_blocks):
            chunk = chunk.replace(f"__CODE_BLOCK_{i}__", code_block)

        # 处理表情包引用，格式{{表情包名称}}
        if meme_match := re.search(r"\{\{(\w+?)\}\}", chunk):
            chunk = chunk.replace(meme_match.group(0), "")
            meme_name = meme_match.group(1)
            if meme_name in chat_configs.instance.memes:
                meme_path = cfg.memes_dir_path / meme_name
                if meme_path.is_dir():
                    image_files = list(meme_path.glob("*"))
                    if image_files:
                        selected_image = random.choice(image_files)
                        segments.append(OneBotMessageSegmentMeme(selected_image))

        # 处理图片链接，格式 ![描述](图片链接)
        elif image_match := re.search(r"!\[.*?\]\((.*?)\)", chunk):
            chunk = chunk.replace(image_match.group(0), "")
            image_url = image_match.group(1)
            segments.append(MessageSegment.image(image_url))

        # 处理后的文本块，如果不为空，则添加为文本消息段
        if chunk.strip():
            segments.append(MessageSegment.text(chunk.strip()))

    return segments


async def send_message_in_chunks(
    matcher: type[Matcher],
    bot: Bot,
    event: Event,
    auto_reply: bool = False,
):
    message = event.get_message()
    onebot = bot if isinstance(bot, OneBot) else None
    prompt, attachments = await parse_message_for_ai(event, onebot)

    # 处理引用（回复）消息
    reply_text: str | None = None
    if isinstance(event, OneBotMessageEvent) and (reply := event.reply):
        reply_text, reply_attachments = await parse_onebot_message_for_ai(reply, onebot)
        attachments.extend(reply_attachments)

    # 进行RAG查询，获取相关文档
    rag_docs: list[str] | None = None
    if cfg.rag.enabled:
        query_str = message.extract_plain_text().strip()
        rag_docs = query(query_str) if query_str else None

    session_info = await extract_session_info(event, bot)

    try:
        contents = await copilot.send_and_wait(
            session_info,
            prompt,
            rag_docs=rag_docs,
            reply_text=reply_text,
            attachments=attachments,
            timeout=300,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("发送消息时发生错误: {}", e)
        # await _send_fail_message(matcher)
        await matcher.finish(f"发送消息时发生错误：{e}")

    if not contents:
        logger.warning(f"会话{session_info.session_id}没有收到任何回复")
        await matcher.finish("没有收到任何回复，请稍后再试")

    # 扣减水晶
    if not auto_reply:
        succeed_consume(
            HandlerKeyEnum.CHAT,
            get_platform_type(event),
            event.get_user_id(),
        )

    if isinstance(event, OneBotMessageEvent):
        all_segments: list[MessageSegment] = []
        for content in contents:
            if not (content := content.strip()):
                continue
            segments = _extract_segments_preserving_code(content)
            all_segments.extend(segments)

        await _send_onebot_message(
            matcher,
            cast(OneBot, bot),
            event,
            all_segments,
            content_long=any(len(content) > 800 for content in contents),
        )
    else:
        for content in contents:
            if not (content := content.strip()):
                continue
            await matcher.send(content)


def should_reply_event(event: Event):
    """确定是否应该回复事件

    用户或群聊在聊天黑名单中->不回复
    群聊中引用了自己的消息，但是没有@ -> 不回复（修改adapter-onebot实现）
    """
    # 确定平台类型
    platform = get_platform_type(event)

    # 检查群聊是否在聊天黑名单中
    ban_type = "group"
    group_id: str | None = None
    if isinstance(event, ConsolePublicMessageEvent):
        group_id = event.channel.id
    elif isinstance(event, OneBotGroupMessageEvent):
        group_id = str(event.group_id)

    if group_id and is_banned(group_id, ban_type, platform):
        return False

    # 检查用户是否在聊天黑名单中
    ban_type = "user"
    user_id: str = event.get_user_id()
    return not (user_id and is_banned(user_id, ban_type, platform))


def should_auto_reply(group_id: str, platform: PlatformType, session_id: str):
    if is_banned(group_id, "group", platform):
        return False

    group_config = chat_configs.instance.get_by_platform(platform).auto_reply_group_config

    # 无配置项，默认不自动回复
    if group_id not in group_config:
        return False
    auto_reply_config = group_config[group_id]

    size = copilot.get_session_messages_size(session_id)
    threshold = auto_reply_config.threshold
    # 阈值小于等于0，或当前消息数小于阈值，不触发自动回复
    if threshold <= 0 or size < threshold:
        return False

    # 达到阈值，按照概率决定是否自动回复
    # 生成一个0.0到1.0之间的随机数，如果小于配置的概率，则触发自动回复
    return random.random() < auto_reply_config.probability
