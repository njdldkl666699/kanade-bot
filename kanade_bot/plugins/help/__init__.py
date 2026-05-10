from pathlib import Path

from nonebot import get_plugin_config, on_command
from nonebot.adapters import Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from kanade_bot.utils.onebot11 import get_onebot_info

from .config import Config
from .help import DOC_NAMES, ensure_help_image, get_help_md

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


help_command = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)


@help_command.handle()
async def _(bot: ConsoleBot, arg_msg: Message = CommandArg()):
    doc_name = arg_msg.extract_plain_text().strip()
    if doc_name not in DOC_NAMES:
        doc_name = "index"

    help_md = get_help_md(doc_name)
    if not help_md:
        help_md = "帮助文档不可用"

    await help_command.finish(ConsoleMessageSegment.markdown(help_md))


@help_command.handle()
async def _(bot: OneBot, arg_msg: Message = CommandArg()):
    segments = OneBotMessage()

    doc_name = arg_msg.extract_plain_text().strip()
    if not doc_name:
        # 发haruki的帮助图片
        segments.append(OneBotMessageSegment.image(Path(cfg.help_haruki_image_file_path)))

    if doc_name not in DOC_NAMES:
        doc_name = "index"
    help_image = await ensure_help_image(doc_name)
    if not help_image:
        segments.append("KanadeBot帮助文档不可用")
    else:
        segments.append(OneBotMessageSegment.image(help_image))

    if len(segments) == 1:
        # 直接发
        await help_command.finish(segments)

    # 发合并转发消息
    bot_id, bot_nickname = await get_onebot_info(bot)
    node_custom_message = OneBotMessage()
    for segment in segments:
        node_custom_message += OneBotMessageSegment.node_custom(
            bot_id, bot_nickname, OneBotMessage(segment)
        )
    await help_command.finish(node_custom_message)
