from typing import Any

from nonebot import get_plugin_config, on_command, require
from nonebot.adapters import Bot, Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import Message as ConsoleMessage
from nonebot.adapters.console import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.message import event_postprocessor
from nonebot.params import CommandArg, EventMessage
from nonebot.plugin import PluginMetadata
from nonechat import ConsoleMessage as NoneChatConsoleMessage
from nonechat.model import Channel

from ..util import extract_session_info
from .config import Config
from .summarizer import summarizer

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import md_to_pic

__plugin_meta__ = PluginMetadata(
    name="summary",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


@event_postprocessor
async def record_recv_msg(
    event: OneBotMessageEvent | ConsoleMessageEvent,
    message: OneBotMessage | ConsoleMessage = EventMessage(),
):
    message_str = ""
    if isinstance(message, OneBotMessage):
        message_str = message.to_rich_text()
    if isinstance(message, ConsoleMessage):
        message_str = str(message)

    session_id, nickname, _ = extract_session_info(event)
    if nickname:
        message_str = f"$ {nickname} ：{message_str}"

    summarizer.add_message(session_id, message_str)


@Bot.on_called_api
async def record_send_msg(
    bot: Bot,
    e: Exception | None,
    api: str,
    data: dict[str, Any],
    result: Any,
):
    if e or not result:
        return
    if api not in ["send_msg", "send_private_msg", "send_group_msg"]:
        return

    if isinstance(bot, OneBot):
        if api == "send_group_msg" or (
            api == "send_msg"
            and (
                data.get("message_type") == "group"
                or (data.get("message_type") is None and data.get("group_id"))
            )
        ):
            session_id = f"qq-group-{data['group_id']}"
        else:
            session_id = f"qq-private-{data['user_id']}"

        message: OneBotMessage = data["message"]
        message_str = f"$ {cfg.summary_bot_name} ：{message.to_rich_text()}"

    elif isinstance(bot, ConsoleBot):
        if api != "send_msg":
            return

        channel: Channel = data["channel"]
        if channel.id.startswith("private:"):
            session_id = f"console-private-{channel.id[8:]}"
        else:
            session_id = f"console-group-{channel.id}"

        elements: NoneChatConsoleMessage = data["content"]
        console_message = ConsoleMessage.from_console_message(elements)
        message_str = f"$ {cfg.summary_bot_name} ：{console_message}"
    else:
        return

    summarizer.add_message(session_id, message_str)


summarize = on_command(
    "总结",
    aliases={"summarize", "summary"},
    priority=2,
    block=True,
)


@summarize.handle()
async def _(
    bot: OneBot | ConsoleBot,
    event: OneBotMessageEvent | ConsoleMessageEvent,
    arg_msg: Message = CommandArg(),
):
    size = arg_msg.extract_plain_text().strip()
    min = cfg.summary_min_size
    max = cfg.summary_max_size

    if not size.isdigit():
        await summarize.finish(f"请提供消息条数，范围 {min}-{max}")

    size = int(size)
    if size < min or size > max:
        await summarize.finish(f"消息条数必须在 {min}-{max} 范围内")

    session_id, nickname, is_group = extract_session_info(event)

    group_or_user_name = nickname
    # 如果是群聊，则修改为群名称
    if is_group:
        if isinstance(bot, OneBot) and isinstance(event, OneBotGroupMessageEvent):
            group_info = await bot.get_group_info(group_id=event.group_id)
            group_or_user_name: str | None = group_info.get("group_name")
        if isinstance(event, ConsoleMessageEvent):
            group_or_user_name = event.channel.name

    summary = await summarizer.summarize(
        session_id,
        size,
        is_group=is_group,
        group_or_user_name=group_or_user_name,
        timeout=300,
    )
    if not summary:
        await summarize.finish("总结失败")

    if not isinstance(bot, OneBot):
        # Console消息直接发送总结文本
        await summarize.finish(summary)

    # OneBot消息发送图片
    image = await md_to_pic(summary)
    await summarize.finish(OneBotMessageSegment.image(image))
