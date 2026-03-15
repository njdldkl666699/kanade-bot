from datetime import datetime
from pathlib import Path

from copilot import (
    CopilotClient,
    CopilotSession,
    MessageOptions,
    PermissionHandler,
    SessionConfig,
    define_tool,
)
from loguru import logger
from nonebot import get_driver, get_plugin_config

from .config import Config

cfg = get_plugin_config(Config)


@define_tool(description="获取现在的本地日期时间，格式为yyyy-MM-dd HH:mm:ss")
def get_datetime_now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class CopilotSessionManager:
    def __init__(self):
        self._client = CopilotClient()
        self.__config: SessionConfig = {
            "model": cfg.chat_model,
            "on_permission_request": PermissionHandler.approve_all,
            "system_message": {
                "mode": "replace",
                "content": Path(cfg.chat_system_message_path).read_text(encoding="utf-8"),
            },
            "tools": [get_datetime_now],
        }
        self.__sessions: dict[str, CopilotSession] = {}
        # 会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表
        self.__sessions_prompt_buffer: dict[str, list[str]] = {}

    async def send_and_wait(
        self, session_id: str, options: MessageOptions, timeout: float | None = None
    ):
        """发送消息并等待响应"""
        # 如果会话不存在，则创建新会话
        if session_id not in self.__sessions:
            self.__sessions[session_id] = await self._client.create_session(self.__config)

        # 将消息缓冲区中的消息添加到选项中
        buffered_messages = self.__sessions_prompt_buffer.get(session_id, [])
        if buffered_messages:
            options["prompt"] = (
                f"$下面是上次对话和这次对话之间的消息：\n{'\n'.join(buffered_messages)}\n\n"
                + f"$下面是这次的用户消息：\n{options['prompt']}"
            )
            # 清空消息缓冲区
            self.__sessions_prompt_buffer[session_id] = []

        return await self.__sessions[session_id].send_and_wait(options, timeout)

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        if session_id not in self.__sessions_prompt_buffer:
            self.__sessions_prompt_buffer[session_id] = []

        self.__sessions_prompt_buffer[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """重置会话"""
        # 断开并删除现有会话
        if session_id in self.__sessions:
            await self.__sessions[session_id].disconnect()
            del self.__sessions[session_id]
        # 清空消息缓冲区
        if session_id in self.__sessions_prompt_buffer:
            del self.__sessions_prompt_buffer[session_id]


copilot = CopilotSessionManager()


driver = get_driver()


@driver.on_startup
async def startup():
    await copilot._client.start()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    await copilot._client.stop()
    logger.info("Copilot客户端已关闭")
