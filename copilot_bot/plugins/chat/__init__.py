from nonebot import on_command, on_fullmatch, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import (
    MessageEvent as OneBotMessageEvent,
    GroupMessageEvent as OneBotGroupMessageEvent,
)
from nonebot.adapters.console.event import (
    MessageEvent as ConsoleMessageEvent,
    PublicMessageEvent as ConsolePublicMessageEvent,
)
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.console.bot import Bot as ConsoleBot
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import EventPlainText, EventToMe
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config
from .copilot import copilot

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="",
    usage="",
    config=Config,
)


ciallo = on_fullmatch(
    ("Ciallo", "Ciallo～(∠・ω< )⌒☆", "Ciallo～(∠・ω< )⌒★"),
    priority=2,
    ignorecase=True,
    block=True,
)


@ciallo.handle()
async def handle_ciallo_console(bot: ConsoleBot):
    await ciallo.finish("Ciallo～(∠・ω< )⌒☆")


@ciallo.handle()
async def handle_ciallo_onebot(bot: OneBot):
    message = MessageSegment(
        "image",
        {
            "file": "2BD9A9D9F906F1B83A5886FA6660C8C0.jpg",
            "summary": "&#91;动画表情&#93;",
            "sub_type": 1,
        },
    )

    await ciallo.finish(message)


def resolve_session_id_and_prompt(event: Event, prompt: str) -> tuple[str, str]:
    """解析事件以获取会话ID和提示词"""
    session_id = event.get_session_id()
    nickname: str | None = None

    # 处理OneBot消息事件
    if isinstance(event, OneBotMessageEvent):
        nickname = event.sender.nickname
    if isinstance(event, OneBotGroupMessageEvent):
        session_id = str(event.group_id)

    # Console的消息事件
    if isinstance(event, ConsoleMessageEvent):
        nickname = event.user.nickname
    if isinstance(event, ConsolePublicMessageEvent):
        session_id = event.channel.id

    prompt = f"{nickname}说：{prompt}" if nickname else prompt
    return session_id, prompt


### 聊天命令
chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


@chat.handle()
async def handle_chat(event: Event, prompt: str = EventPlainText()):
    session_id, prompt = resolve_session_id_and_prompt(event, prompt)
    response = await copilot.send_and_wait(session_id, {"prompt": prompt})
    if response:
        await chat.finish(response.data.content)
    else:
        await chat.finish("模型未响应，请稍后再试")


def not_to_me(to_me: bool = EventToMe()):
    return not to_me


### 聊天监听命令
# 用于监听非@消息并将其添加到会话缓冲区，以便在下一次@消息时一起发送给模型
chat_monitor = on_message(
    rule=not_to_me,
    priority=1,
    block=False,
)


@chat_monitor.handle()
async def handle_chat_monitor(event: Event, prompt: str = EventPlainText()):
    session_id, prompt = resolve_session_id_and_prompt(event, prompt)

    # 将用户消息添加到会话缓冲区
    await copilot.add_message(session_id, prompt)


### 重置会话命令
chat_reset = on_command(
    "重置会话",
    aliases={"chat_reset", "chatreset", "重置对话"},
    priority=2,
    block=True,
)


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    session_id, _ = resolve_session_id_and_prompt(event, "")

    await copilot.reset_session(session_id)
    await chat_reset.finish("会话已重置")
