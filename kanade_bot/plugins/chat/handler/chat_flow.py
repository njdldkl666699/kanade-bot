"""被动回复与旁观监听：@消息驱动的回复轮次与群消息缓冲"""

from contextlib import aclosing
from pathlib import Path
from typing import cast

from nonebot import logger, require
from nonebot.adapters import Bot, Event
from nonebot.adapters.console.event import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.matcher import Matcher

from kanade_bot.utils.common import asia_shanghai_now, asia_shanghai_now_str, get_platform_type
from kanade_bot.utils.onebot11 import OneBotMessageSegmentMeme
from kanade_bot.utils.parse import (
    TextFormat,
    build_sender_info,
    guess_format,
    parse_message_for_ai,
    parse_onebot_message_for_ai,
)
from kanade_bot.utils.session import extract_session_info

from ..agent.manager import chat_manager
from ..agent.proactive import auto_reply_runtime
from ..config import cfg
from ..deliver import parse_message_segments, send_segments
from ..matcher import chat, chat_monitor
from ..util import group_id_of, is_banned, make_turn_biller

require("crystal")
from kanade_bot.plugins.crystal import HandlerKeyEnum, check_user_crystal, finish_fail_consume

if cfg.rag.enabled:
    from ..rag import query
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

    turn_start = asia_shanghai_now()
    # 轮次正常结束时按实际usage计费（有文本产出才扣费，峰谷按本轮用户消息时间判定）
    bill_usage = make_turn_biller(get_platform_type(event), event.get_user_id(), turn_start)

    replied = False
    try:
        # 流式消费：每条助手消息一到达就立即处理发送，无需等待全部生成完毕。
        # aclosing确保中途异常退出时也会关闭生成器：
        # 退订事件、清空消息缓冲区、释放会话锁
        async with aclosing(
            chat_manager.get_session(session_info.session_id).send_and_wait(
                session_info,
                prompt,
                bot_id=bot.self_id,
                rag_docs=rag_docs,
                reply_text=reply_text,
                images=attachments,
                timeout=600,
                on_usage=bill_usage,
            )
        ) as contents:
            async for content in contents:
                if not (content := content.strip()):
                    continue
                # 收到非空回复，标记已回复（否则结束后会误报“没有收到任何回复”）
                replied = True

                if isinstance(event, OneBotMessageEvent):
                    segments = await parse_message_segments(content)
                    await _send_onebot_message(
                        matcher,
                        cast(OneBot, bot),
                        event,
                        segments,
                        content_long=len(content) > 600 or len(content.splitlines()) > 20,
                        content_format=guess_format(content),
                        first_reply=isinstance(event, OneBotGroupMessageEvent),
                    )
                else:
                    await matcher.send(content)
                # bot 发言 append 进主动回复决策历史
                await auto_reply_runtime.on_bot_message(session_info.session_id, content)
    except Exception as e:
        logger.exception("发送消息时发生错误: {}", e)
        # await _send_fail_message(matcher)
        await matcher.finish(f"发送消息时发生错误：{e}")

    if not replied:
        logger.warning(f"会话{session_info.session_id}没有收到任何回复")
        await matcher.finish("没有收到任何回复，请稍后再试")


def should_reply_event(event: Event) -> bool:
    """确定是否应该回复事件

    不回复情况：

    - 用户或群聊在聊天黑名单中
    - 修改adapter-onebot实现
      - 群聊中引用了Bot的消息，但是没有@Bot或以Bot名称开头
      - 私聊中没有以Bot名称开头
    """
    # 确定平台类型
    platform = get_platform_type(event)

    # 检查群聊是否在聊天黑名单中
    if (group_id := group_id_of(event)) and is_banned(group_id, "group", platform):
        return False

    # 检查用户是否在聊天黑名单中
    user_id: str = event.get_user_id()
    return not (user_id and is_banned(user_id, "user", platform))


@chat.handle()
async def handle_chat(bot: Bot, event: OneBotMessageEvent | ConsoleMessageEvent):
    if not should_reply_event(event):
        return

    key = HandlerKeyEnum.CHAT
    platform = get_platform_type(event)
    user_id = event.get_user_id()
    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(chat, key, platform, user_id)

    await send_message_in_chunks(chat, bot, event)


@chat_monitor.handle()
async def handle_chat_monitor(bot: Bot, event: Event):
    session_info = await extract_session_info(event, bot)
    session_id = session_info.session_id
    platform = get_platform_type(event)

    group_id = group_id_of(event)

    # 添加消息到会话缓冲区，不需要图片
    message_str, _ = await parse_message_for_ai(event)
    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        message_str = f"[{asia_shanghai_now_str('%Y-%m-%d %H:%M')}] {user_info}：{message_str}"
    await chat_manager.get_session(session_id).add_message(message_str)

    # 主动回复流水线：消息 append 决策历史 + 防抖观察
    if group_id and session_info.group_name and auto_reply_runtime.is_enabled(group_id, platform):
        await auto_reply_runtime.on_group_message(
            session_id,
            group_id,
            message_str,
            author=session_info.user_id,
            bot=bot,
            session_info=session_info,
        )
