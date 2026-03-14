from nonebot import on_command, on_message
from nonebot.adapters import Event
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.params import EventPlainText, EventToMe
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config
from .session import copilot

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="",
    usage="",
    config=Config,
)

### 聊天命令
chat = on_message(
    rule=to_me(),
    priority=100000,
    block=True,
)


@chat.handle()
async def handle_chat(event: Event, prompt: str = EventPlainText()):
    # OneBot 消息事件，添加发送者昵称到提示词中
    if isinstance(event, OneBotMessageEvent):
        prompt = f"{event.sender.nickname}说：{prompt}"

    response = await copilot.send_and_wait(event.get_session_id(), {"prompt": prompt})
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
    # OneBot 消息事件，添加发送者昵称到提示词中
    if isinstance(event, OneBotMessageEvent):
        prompt = f"{event.sender.nickname}说：{prompt}"

    # 将用户消息添加到会话缓冲区
    await copilot.add_message(event.get_session_id(), prompt)


### 重置会话命令
chat_reset = on_command(
    "重置会话",
    aliases={"chat_reset", "chatreset", "重置对话"},
    priority=2,
    block=True,
)


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    await copilot.reset_session(event.get_session_id())
    await chat_reset.finish("会话已重置")
