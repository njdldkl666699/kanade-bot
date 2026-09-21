from io import BytesIO
from pathlib import Path
from typing import Literal, override

from nonebot import get_plugin_config
from nonebot.adapters.onebot.v11 import Bot, MessageSegment, NoticeEvent

from .schema import KanadeConfig


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


async def get_bot_info(bot: Bot) -> tuple[int, str]:
    """获取OneBot机器人的ID和昵称"""
    bot_id = int(bot.self_id)
    bot_info = await bot.get_stranger_info(user_id=bot_id)
    bot_nickname: str = bot_info.get("nickname", "宵崎奏")
    return bot_id, bot_nickname


async def get_image_path(bot: Bot, image_segment: MessageSegment) -> Path:
    """获取OneBot图片消息段对应的本地图片路径"""
    assert image_segment.type == "image", "消息段必须是图片类型"

    from .common import HTTPX_CLIENT

    file: str = image_segment.data["file"]
    r = await bot.get_image(file=file)
    r_file = r["file"]
    if not r_file.startswith(("http://", "https://")):
        return Path(r_file)

    # 若get_image返回的是网络路径，则下载到本地缓存目录
    url = r_file
    cache_dir = get_plugin_config(KanadeConfig).autoclear_cache_dir_path
    pic_path = cache_dir / file
    r = await HTTPX_CLIENT.get(url)
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


async def upload_group_file(
    bot: Bot,
    group_id: int,
    file_path: Path,
    name: str | None = None,
    folder: str | None = None,
):
    """上传群文件

    :param group_id: 群号
    :param file_path: 本地文件路径
    :param name: 文件名（不包含路径），默认为file_path的文件名
    :param folder: 上传到群文件的目录，默认为根目录
    """
    if not name:
        name = file_path.name
    return await bot.call_api(
        "upload_group_file",
        group_id=group_id,
        file=str(file_path),
        name=name,
        folder=folder or "",
    )


async def upload_private_file(
    bot: Bot,
    user_id: int,
    file_path: Path,
    name: str | None = None,
):
    """上传私聊文件

    :param user_id: 用户ID
    :param file_path: 本地文件路径
    :param name: 文件名（不包含路径），默认为file_path的文件名
    """
    if not name:
        name = file_path.name
    return await bot.call_api(
        "upload_private_file",
        user_id=user_id,
        file=str(file_path),
        name=name,
    )
