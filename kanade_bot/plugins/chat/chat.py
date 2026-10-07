import random
from contextlib import aclosing
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
from pydantic_ai.usage import RunUsage

from kanade_bot.utils.billing import compute_token_cost, is_peak_hours
from kanade_bot.utils.common import PlatformType, asia_shanghai_now, get_platform_type
from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme
from kanade_bot.utils.parse import (
    TextFormat,
    guess_format,
    parse_message_for_ai,
    parse_onebot_message_for_ai,
)
from kanade_bot.utils.session import extract_session_info

from .agent.manager import chat_manager
from .ban import is_banned
from .config import cfg, chat_configs
from .deliver import extract_segments_preserving_code, send_segments

require("crystal")
from kanade_bot.plugins.crystal import consume_crystal

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
    content_format: TextFormat = "plaintext",
    first_reply: bool = False,
):
    """把消息段列表发送回事件来源会话（引用回复仅拼在第一条消息前）"""

    async def _send(message: OneBotMessage | MessageSegment | str) -> None:
        await matcher.send(message)

    await send_segments(
        _send,
        bot,
        segments,
        reply=MessageSegment.reply(event.message_id) if first_reply else None,
        content_long=content_long,
        content_format=content_format,
    )


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

    def _bill_usage(usage: RunUsage, produced: bool) -> None:
        """轮次正常结束时按实际usage计费（扣费允许至负数）

        有文本产出且非主动回复才扣费；峰谷按本轮用户消息时间判定。
        """
        if not produced or auto_reply:
            return
        cost = compute_token_cost(usage, cfg.billing, peak=is_peak_hours(turn_start))
        consume_crystal(get_platform_type(event), event.get_user_id(), cost)

    turn_start = asia_shanghai_now()

    replied = False
    try:
        # 流式消费：每条助手消息一到达就立即处理发送，无需等待全部生成完毕。
        # aclosing确保中途异常退出时也会关闭生成器：
        # 退订事件、清空消息缓冲区、释放会话锁
        async with aclosing(
            chat_manager.send_and_wait(
                session_info,
                prompt,
                bot_id=onebot.self_id if onebot else None,
                rag_docs=rag_docs,
                reply_text=reply_text,
                images=attachments,
                timeout=600,
                on_usage=_bill_usage,
            )
        ) as contents:
            async for content in contents:
                if not (content := content.strip()):
                    continue
                # 收到非空回复，标记已回复（否则结束后会误报“没有收到任何回复”）
                replied = True

                if isinstance(event, OneBotMessageEvent):
                    segments = extract_segments_preserving_code(content)
                    await _send_onebot_message(
                        matcher,
                        cast(OneBot, bot),
                        event,
                        segments,
                        content_long=len(content) > 600 or len(content.splitlines()) > 20,
                        content_format=guess_format(content),
                        first_reply=True,
                    )
                else:
                    await matcher.send(content)
    except Exception as e:
        logger.exception("发送消息时发生错误: {}", e)
        # await _send_fail_message(matcher)
        await matcher.finish(f"发送消息时发生错误：{e}")

    if not replied:
        logger.warning(f"会话{session_info.session_id}没有收到任何回复")
        await matcher.finish("没有收到任何回复，请稍后再试")


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

    size = chat_manager.get_session_messages_size(session_id)
    threshold = auto_reply_config.threshold
    # 阈值小于等于0，或当前消息数小于阈值，不触发自动回复
    if threshold <= 0 or size < threshold:
        return False

    # 达到阈值，按照概率决定是否自动回复
    # 生成一个0.0到1.0之间的随机数，如果小于配置的概率，则触发自动回复
    return random.random() < auto_reply_config.probability
