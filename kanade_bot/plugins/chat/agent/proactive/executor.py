"""主动回复执行：唤醒会话接话并逐条投递"""

from collections.abc import Awaitable, Callable
from contextlib import aclosing

from nonebot import logger
from nonebot.adapters import Bot

from kanade_bot.utils.common import asia_shanghai_now_str
from kanade_bot.utils.session import SessionInfo

from ...deliver import send_proactive
from ..manager import chat_manager

type OnContent = Callable[[str], Awaitable[None]]
"""每条助手产出投递后的回调（决策历史记录等）"""

PROACTIVE_NOTIFICATION = "你刚刚在旁观群聊，现在决定主动插话。请自然地接话，简短即可。"
"""主动回复轮的插话场景说明"""

ABORTED_REPLY_NOTE = "<system_notification>上一判定为「回」，但这批消息已被被动回复消费，或本轮没有产出，主动插话未实际发出</system_notification>"


def build_proactive_notification(reason: str | None = None) -> str:
    """主动回复轮系统通知：插话场景说明 + 决策器判定理由（有则附加）"""
    if not reason:
        return PROACTIVE_NOTIFICATION
    return f"{PROACTIVE_NOTIFICATION}\n决策器判定理由：{reason}"


def build_aborted_reply_note() -> str:
    """判「回」未发出的补记行：带时间戳，以 user 行 append 进决策历史"""
    return f"[{asia_shanghai_now_str('%Y-%m-%d %H:%M')}] {ABORTED_REPLY_NOTE}"


async def send_proactive_reply(
    bot: Bot,
    session_info: SessionInfo,
    *,
    reason: str | None = None,
    on_content: OnContent | None = None,
) -> bool:
    """主动回复执行

    :param reason: 决策器判定「回」的理由，随系统通知注入提示词供接话时参考
    :param on_content: 每条产出投递后的回调（决策历史 append 等），由调用方注入
    :return: 是否实际发出至少一条回复（False 时调用方补记未发出事实行）
    """
    session = chat_manager.get_session(session_info.session_id)
    if not session.buffer:
        # 被动轮恰好先消费了 buffer：无新内容可说，直接放弃
        # （快速路径；真正开始轮次时还会在会话锁内复检，见 send_and_wait 的 require_buffer）
        logger.info(f"主动回复执行时 buffer 已空，跳过会话{session_info.session_id}")
        return False

    sent_any = False
    async with aclosing(
        session.send_and_wait(
            session_info,
            "",
            bot_id=bot.self_id,
            system_notification=build_proactive_notification(reason),
            require_buffer=True,
        )
    ) as contents:
        async for content in contents:
            if content := content.strip():
                await send_proactive(bot, session_info, content)
                sent_any = True
                if on_content is not None:
                    await on_content(content)
    return sent_any
