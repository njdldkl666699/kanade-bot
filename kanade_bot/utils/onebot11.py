from io import BytesIO
from pathlib import Path
from typing import Literal, override

from nonebot.adapters.onebot.v11 import Bot, MessageSegment, NoticeEvent


def OneBotMessageSegmentMeme(file: str | bytes | BytesIO | Path) -> MessageSegment:
    """创建一个OneBot动画表情消息段"""
    message = MessageSegment.image(file)
    message.data["summary"] = "[动画表情]"
    message.data["sub_type"] = 1
    return message


async def set_msg_emoji_like(
    bot: Bot,
    message_id: int,
    emoji_id: int,
    set: bool = True,
):
    """设置表情回复

    :param message_id: 消息ID
    :param emoji_id: 表情ID
    """
    await bot.call_api(
        "set_msg_emoji_like",
        message_id=message_id,
        emoji_id=emoji_id,
        set=set,
    )


async def send_poke(
    bot: Bot,
    user_id: int | str,
    group_id: int | None = None,
):
    """发送戳一戳

    :param user_id: 目标用户ID
    :param group_id: 群聊ID（如果是群聊内戳人则需要提供）
    """
    await bot.call_api(
        "send_poke",
        user_id=user_id,
        group_id=group_id,
    )


async def get_onebot_info(bot: Bot) -> tuple[int, str]:
    """获取OneBot机器人的ID和昵称"""
    bot_id = int(bot.self_id)
    bot_info = await bot.get_stranger_info(user_id=bot_id)
    bot_nickname: str = bot_info.get("nickname", "宵崎奏")
    return bot_id, bot_nickname


class BotOfflineNoticeEvent(NoticeEvent):
    """Bot掉线通知事件"""

    notice_type: Literal["bot_offline"]  # pyright: ignore[reportIncompatibleVariableOverride]
    user_id: int
    tag: str
    message: str

    @override
    def get_user_id(self) -> str:
        return str(self.user_id)

    @override
    def get_session_id(self) -> str:
        return str(self.user_id)
