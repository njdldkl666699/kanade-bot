import asyncio
from pathlib import Path

from copilot import CopilotClient, PermissionHandler, SessionConfig
from nonebot import get_plugin_config, on_message
from nonebot.params import EventPlainText
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

chat = on_message(rule=to_me(), priority=100000, block=True)


class CopilotSessionSingleton:
    _instance = None
    __copilot_client = CopilotClient()
    __session_config: SessionConfig = {
        "model": config.chat_model,
        "on_permission_request": PermissionHandler.approve_all,
        "system_message": {
            "mode": "replace",
            "content": Path(config.chat_system_message_path).read_text(encoding="utf-8"),
        },
    }

    @classmethod
    async def get_instance(cls):
        if cls._instance is None:
            cls._instance = await CopilotSessionSingleton.__copilot_client.create_session(
                CopilotSessionSingleton.__session_config
            )
        return cls._instance

    async def __del__(self):
        if self._instance is not None:
            asyncio.create_task(self._instance.disconnect())
            await CopilotSessionSingleton.__copilot_client.stop()


@chat.handle()
async def handle_chat(message: str = EventPlainText()):
    session = await CopilotSessionSingleton.get_instance()
    response = await session.send_and_wait({"prompt": message})
    if response:
        await chat.finish(response.data.content)
    else:
        await chat.finish("模型未响应，请稍后再试")
