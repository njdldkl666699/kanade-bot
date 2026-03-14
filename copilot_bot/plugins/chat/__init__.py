from nonebot import on_command, on_message
from nonebot.adapters import Event
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

chat = on_message(rule=to_me(), priority=100000, block=True)


@chat.handle()
async def handle_chat(event: Event, message: str = EventPlainText()):
    response = await copilot.send_and_wait(event.get_session_id(), {"prompt": message})
    if response:
        await chat.finish(response.data.content)
    else:
        await chat.finish("模型未响应，请稍后再试")


def not_to_me(to_me: bool = EventToMe()):
    return not to_me


chat_monitor = on_message(rule=not_to_me, priority=1, block=False)


@chat_monitor.handle()
async def handle_chat_monitor(event: Event, message: str = EventPlainText()):
    # 将用户消息添加到会话缓冲区
    await copilot.add_message(event.get_session_id(), message)


chat_reset = on_command(
    "重置对话",
    rule=to_me(),
    aliases={"chat_reset", "chatreset", "重置会话"},
    priority=2,
    block=True,
)


@chat_reset.handle()
async def handle_chat_reset(event: Event):
    await copilot.reset_session(event.get_session_id())
    await chat_reset.finish("会话已重置")
