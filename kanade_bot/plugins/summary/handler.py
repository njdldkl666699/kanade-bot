from typing import Any

from nonebot import get_plugin_config, logger, require
from nonebot.adapters import Bot, Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import Message as ConsoleMessage
from nonebot.adapters.console import MessageEvent as ConsoleMessageEvent
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.exception import ActionFailed
from nonebot.message import event_postprocessor
from nonebot.params import CommandArg
from nonechat import ConsoleMessage as NoneChatConsoleMessage
from nonechat.model import Channel

from kanade_bot.utils.common import get_platform_type
from kanade_bot.utils.parse import build_sender_info, parse_message_for_ai
from kanade_bot.utils.session import extract_session_info

from .config import Config
from .matcher import summarize
from .summarizer import summarizer

require("nonebot_plugin_htmlrender")
require("crystal")

from nonebot_plugin_htmlrender import md_to_pic

from kanade_bot.plugins.crystal import (
    HandlerKeyEnum,
    check_user_crystal,
    finish_fail_consume,
    succeed_consume,
)

cfg = get_plugin_config(Config).summary


@event_postprocessor
async def record_recv_msg(event: OneBotMessageEvent | ConsoleMessageEvent):
    message_str, _ = await parse_message_for_ai(event)
    session_info = await extract_session_info(event)
    if user_info := build_sender_info(session_info.nickname, session_info.user_id):
        message_str = f"$ {user_info}：{message_str}"

    summarizer.add_message(session_info.session_id, message_str)


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

        message: str | OneBotMessage = data["message"]
        message_str = f"$ {cfg.bot_name} ："
        if isinstance(message, OneBotMessage):
            message_str = message_str + message.to_rich_text()
        else:
            message_str = message_str + message

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
        message_str = f"$ {cfg.bot_name} : {console_message}"
    else:
        return

    summarizer.add_message(session_id, message_str)


@summarize.handle()
async def _(
    bot: OneBot | ConsoleBot,
    event: OneBotMessageEvent | ConsoleMessageEvent,
    arg_msg: Message = CommandArg(),
):
    key = HandlerKeyEnum.SUMMARIZE
    platform = get_platform_type(event)
    user_id = event.get_user_id()

    if not check_user_crystal(key, platform, user_id):
        await finish_fail_consume(summarize, key, platform, user_id)

    size = arg_msg.extract_plain_text().strip()
    min = cfg.min_size
    max = cfg.max_size

    if not size.isdigit():
        await summarize.finish(f"请提供消息条数，范围 {min}-{max}")

    size = int(size)
    if size < min or size > max:
        await summarize.finish(f"消息条数必须在 {min}-{max} 范围内")

    session_info = await extract_session_info(event, bot)

    # 如果是群聊，则修改为群名称
    group_name = session_info.group_name
    group_or_user_name = group_name or session_info.nickname

    response = await summarize.send("正在总结中，请稍候...")
    # 准备总结消息的任务
    summary_future = summarizer.summarize(
        session_info.session_id,
        size,
        is_group=bool(group_name),
        group_or_user_name=group_or_user_name,
        timeout=300,
    )

    if isinstance(bot, OneBot):

        async def try_delete_msg():
            try:
                await bot.delete_msg(message_id=response["message_id"])
            except ActionFailed as e:
                logger.warning("删除总结提示消息失败: {}", e)

        summary = await summary_future
        if not summary:
            await try_delete_msg()
            await summarize.finish("总结失败")

        image = await md_to_pic(summary)
        await try_delete_msg()
        succeed_consume(key, platform, user_id)
        await summarize.finish(OneBotMessageSegment.image(image))

    elif isinstance(bot, ConsoleBot):
        summary = await summary_future
        await bot.recall_message(
            message_id=response.message_id,
            channel_id=response.channel_id,
        )

        if not summary:
            await summarize.finish("总结失败")

        succeed_consume(key, platform, user_id)
        await summarize.finish(summary)
