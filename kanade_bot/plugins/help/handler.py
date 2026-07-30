import random
from asyncio import subprocess

from httpx import HTTPStatusError
from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters import Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent
from nonebot.adapters.onebot.v11 import Message as OneBotMessage
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg

from kanade_bot.utils.common import HTTPX_CLIENT
from kanade_bot.utils.onebot11 import BotOfflineNoticeEvent
from kanade_bot.utils.parse import build_sender_info

from .config import Config
from .help import DOC_NAMES, ensure_help_image, get_help_md
from .matcher import execute_command, help_command, leave_notice, offline_notice, welcome

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
    doc_name = arg_msg.extract_plain_text().strip()
    if doc_name not in DOC_NAMES:
        doc_name = "index"
    help_image = await ensure_help_image(doc_name)
    if not help_image:
        await help_command.finish("KanadeBot帮助文档不可用")

    await help_command.finish(OneBotMessageSegment.image(help_image))


@offline_notice.handle()
async def _(event: BotOfflineNoticeEvent):
    if not (url := cfg.ntfy_topic_url):
        logger.warning("未配置ntfy topic url，无法发送Bot掉线通知")
        return

    # 构建消息主体
    title = f"{event.tag} 你的Bot掉线了"
    content = (
        f"你的Bot账号: {event.self_id} 掉线了，赶快去看看吧。\n`Message`: {event.message}".encode()
    )
    headers = {"Title": title}

    path = cfg.login_qrcode_file_path
    if path and path.is_file():
        content = path.read_bytes()
        headers["Filename"] = path.name

    # 发送通知
    try:
        response = await HTTPX_CLIENT.put(
            url,
            headers=headers,
            content=content,
        )
        response.raise_for_status()
    except HTTPStatusError as e:
        logger.error(f"发送Bot掉线通知请求时发生异常: {e}")
        return


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


@welcome.handle()
async def _(event: GroupIncreaseNoticeEvent):
    user_id = event.user_id
    if user_id == event.self_id:
        # 不欢迎自己
        return

    template = random.choice(cfg.welcome_message_templates)
    texts = template.split("{nickname}", maxsplit=2)

    message = OneBotMessage()
    for i, text in enumerate(texts):
        message.append(text)
        if i < len(texts) - 1:
            message.append(OneBotMessageSegment.at(user_id))

    if p := cfg.welcome_image_file_path:
        message.append(OneBotMessageSegment.image(p))

    await welcome.finish(message)


@leave_notice.handle()
async def _(bot: OneBot, event: GroupDecreaseNoticeEvent):
    user_id = event.user_id
    r = await bot.get_stranger_info(user_id=user_id)
    user_info_str = build_sender_info(r["nickname"], str(user_id))
    await leave_notice.finish(f"{user_info_str} 离开了群聊...")


driver = get_driver()


@driver.on_bot_connect
async def notify_bot_online(bot: OneBot):
    if not cfg.online_notice_group_ids:
        return

    for group_id in cfg.online_notice_group_ids:
        await bot.send_group_msg(group_id=group_id, message="宵崎奏Bot 已上线")
