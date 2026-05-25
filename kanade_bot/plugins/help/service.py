from asyncio import subprocess

from nonebot import get_driver, get_plugin_config
from nonebot.adapters import Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment

from nonebot.params import CommandArg

from kanade_bot.utils.onebot11 import BotOfflineNoticeEvent, get_onebot_info

from .config import Config
from .help import DOC_NAMES, ensure_help_image, get_help_md
from .matcher import execute_command, help_command, offline_notice
from .offline import send_offline_notice

cfg = get_plugin_config(Config).help


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
        segments.append(OneBotMessageSegment.image(cfg.haruki_image_file_path))

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


@offline_notice.handle()
async def _(event: BotOfflineNoticeEvent):
    bot_id = event.self_id
    tag = event.tag
    message = event.message

    await send_offline_notice(bot_id, tag, message)


@execute_command.handle()
async def _(arg_msg: Message = CommandArg()):
    command = arg_msg.extract_plain_text().strip()
    if not command:
        await execute_command.finish("请输入要执行的命令")

    proc = await subprocess.create_subprocess_shell(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    await execute_command.send(f"stdout: \n{stdout.decode()}")
    await execute_command.finish(f"stderr: \n{stderr.decode()}")


driver = get_driver()


@driver.on_bot_connect
async def notify_bot_online(bot: OneBot):
    if not cfg.online_notice_group_ids:
        return

    for group_id in cfg.online_notice_group_ids:
        await bot.send_group_msg(group_id=group_id, message="宵崎奏Bot 已上线")
