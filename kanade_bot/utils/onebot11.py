from io import BytesIO
from pathlib import Path
from typing import Literal, override

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
)
from nonebot.exception import ActionFailed
from nonebot.matcher import Matcher


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
    return await bot.call_api(
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
    return await bot.call_api(
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


async def get_image_local(bot: Bot, file: str) -> Path:
    """调用bot.get_image()获取file对应的本地图片路径

    部分协议实现可能返回网络路径，此函数会将其下载到本地缓存目录并返回本地路径
    """
    from .common import HTTPX_CLIENT
    from .schema import KanadeConfig

    r = await bot.get_image(file=file)
    file_url = r["file"]
    if not file_url.startswith(("http://", "https://")):
        return Path(file_url)

    # 下载图片到本地缓存目录
    cache_dir = get_plugin_config(KanadeConfig).image_cache_dir_path
    pic_path = cache_dir / file
    r = await HTTPX_CLIENT.get(file_url)
    r.raise_for_status()
    pic_path.write_bytes(r.content)
    return pic_path


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


async def send_forward_msg(
    bot: Bot,
    message_type: Literal["private", "group"],
    group_id: int | None = None,
    user_id: int | None = None,
    messages: Message | None = None,
    message: Message | None = None,
    auto_escape: bool = False,
):
    """发送合并转发消息"""
    return await bot.call_api(
        "send_forward_msg",
        message_type=message_type,
        group_id=group_id,
        user_id=user_id,
        messages=messages,
        message=message,
        auto_escape=auto_escape,
    )


async def ensure_send_forward_message(
    matcher: type[Matcher],
    bot: Bot,
    event: MessageEvent,
    node_custom_message: Message,
):
    try:
        await matcher.send(node_custom_message)
    except ActionFailed:
        # 部分OneBot 11实现不支持使用send_msg发送转发消息，
        # 使用其扩展接口send_forward_msg
        message_type = "private"
        group_id: int | None = None
        user_id = event.user_id
        if isinstance(event, GroupMessageEvent):
            message_type = "group"
            group_id = event.group_id
        await send_forward_msg(
            bot,
            message_type=message_type,
            group_id=group_id,
            user_id=user_id,
            message=node_custom_message,
        )
