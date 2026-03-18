import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

from copilot import (
    CopilotClient,
    CopilotSession,
    MessageOptions,
    PermissionHandler,
    SessionConfig,
    SessionEvent,
)
from copilot.generated.rpc import SessionAgentSelectParams
from loguru import logger
from nonebot import get_driver, get_plugin_config

from copilot_bot.plugins.chat.tool import tavily_search

from .config import Config

cfg = get_plugin_config(Config)

"""
The GitHub CLI enforces a 30‑minute session timeout and will end a session if
there is no human interaction during that window. If there is no user activity
for 30 minutes, the session should be terminated. Whenever there is any human interaction,
the 30‑minute timer should be reset.
GitHub CLI强制执行30分钟的会话超时，如果在30分钟内没有人类交互，CLI将结束会话。
每当有任何人类交互时，30分钟的计时器应该被重置。

为此，我们做两个设计：
1. 我们手动维护一个25分钟的会话超时，超过25分钟没有人类交互就ping一下模型，
保持会话活跃，避免被GitHub CLI自动关闭；
2. 由于ping为定时发送，可能存在send_and_wait加锁后，会话被关闭的情况，
所以在send_and_wait中如果发生错误，我们只能重置会话。

"""


class CopilotSessionManager:
    KEEPALIVE_INTERVAL_SECONDS = 60
    KEEPALIVE_IDLE_THRESHOLD = timedelta(minutes=25)

    def __init__(self):
        self._client = CopilotClient()
        self.__config: SessionConfig = {
            "model": cfg.chat_model,
            "on_permission_request": PermissionHandler.approve_all,
            "custom_agents": [
                {
                    "name": "Kanade",
                    "display_name": "宵崎奏",
                    "description": "宵崎奏人格Agent，始终使用此Agent回复消息。",
                    "tools": ["read", "search", "web", "todo", tavily_search.name],
                    "prompt": Path(cfg.chat_system_message_path).read_text(encoding="utf-8"),
                }
            ],
            "agent": "Kanade",
            "tools": [tavily_search],
            "available_tools": ["read", "search", "web", "todo", tavily_search.name],
        }
        self.__sessions: dict[str, CopilotSession] = {}
        # 会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表
        self.__sessions_prompt_buffer: dict[str, list[str]] = {}
        # 记录每个会话最近一次人类交互时间（UTC）
        self.__last_human_interaction: dict[str, datetime] = {}
        # 每个会话的异步锁，避免用户请求和保活ping并发发送
        self.__session_locks: dict[str, asyncio.Lock] = {}
        self.__keepalive_task: asyncio.Task | None = None

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)

    def _touch_human_interaction(self, session_id: str):
        self.__last_human_interaction[session_id] = self._utcnow()

    def _get_session_lock(self, session_id: str) -> asyncio.Lock:
        if session_id not in self.__session_locks:
            self.__session_locks[session_id] = asyncio.Lock()
        return self.__session_locks[session_id]

    async def _create_session(self, session_id: str) -> CopilotSession:
        session = await self._client.create_session(self.__config)
        await session.rpc.agent.select(SessionAgentSelectParams("Kanade"))
        self.__sessions[session_id] = session
        return session

    async def _ping_if_idle(self, session_id: str):
        lock = self._get_session_lock(session_id)
        async with lock:
            last_interaction = self.__last_human_interaction.get(session_id)
            if last_interaction is None:
                # 如果会话存在，则不应该出现这种情况，兜底
                return

            idle_duration = self._utcnow() - last_interaction
            if idle_duration < self.KEEPALIVE_IDLE_THRESHOLD:
                return

            session = self.__sessions.get(session_id)
            if session is None:
                return

            try:
                logger.info(f"会话{session_id}空闲 {idle_duration}，发送保活ping")
                await session.send_and_wait({"prompt": "$ping"}, timeout=15)
            except Exception as e:
                logger.warning(f"会话{session_id}保活ping失败: {e}")
                return

            # ping成功后刷新人类交互时间，避免下个巡检周期再次ping
            self._touch_human_interaction(session_id)

    async def _keepalive_loop(self):
        try:
            while True:
                await asyncio.sleep(self.KEEPALIVE_INTERVAL_SECONDS)
                if not self.__sessions:
                    continue
                for session_id in list(self.__sessions.keys()):
                    await self._ping_if_idle(session_id)
        except asyncio.CancelledError:
            logger.debug("Copilot会话保活任务已停止")
            raise

    def start_keepalive(self):
        if self.__keepalive_task is None or self.__keepalive_task.done():
            self.__keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="copilot-session-keepalive",
            )

    async def stop_keepalive(self):
        if self.__keepalive_task is None:
            return
        self.__keepalive_task.cancel()
        try:
            await self.__keepalive_task
        except asyncio.CancelledError:
            pass
        self.__keepalive_task = None

    async def send_and_wait(
        self,
        session_id: str,
        options: MessageOptions,
        timeout: float | None = None,
        is_group: bool = False,
    ) -> tuple[SessionEvent | None, bool]:
        """发送消息并等待响应，返回响应事件和是否是新会话"""
        lock = self._get_session_lock(session_id)
        async with lock:
            # 如果会话不存在，则创建新会话
            if session_id not in self.__sessions:
                await self._create_session(session_id)

            # 将消息缓冲区中的消息添加到选项中
            buffered_messages = self.__sessions_prompt_buffer.get(session_id, [])
            if buffered_messages:
                options["prompt"] = (
                    ("$现在的会话是群聊\n" if is_group else "")
                    + f"$下面是上次对话和这次对话之间的消息：\n{'\n'.join(buffered_messages)}\n\n"
                    + f"$下面是这次的用户消息：\n{options['prompt']}"
                )
                # 清空消息缓冲区
                self.__sessions_prompt_buffer[session_id] = []

            # 发送用户消息前视为发生了人类交互
            self._touch_human_interaction(session_id)

            session_event: SessionEvent | None = None
            new_session = False
            session = self.__sessions[session_id]
            try:
                session_event = await session.send_and_wait(options, timeout)
            except Exception as e:
                # 见(说明2.)
                logger.warning(f"发送消息或等待响应时发生错误: {e}")
                # 重置会话并重试一次
                await self.clear_session(session_id)
                session = await self._create_session(session_id)
                session_event = await session.send_and_wait(options, timeout)
                new_session = True

            return session_event, new_session

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        if session_id not in self.__sessions_prompt_buffer:
            self.__sessions_prompt_buffer[session_id] = []

        self.__sessions_prompt_buffer[session_id].append(prompt)

    async def clear_session(self, session_id: str):
        """断开并删除会话，清空缓冲区和交互记录"""
        # 断开并删除现有会话
        if session_id in self.__sessions:
            await self.__sessions[session_id].disconnect()
            del self.__sessions[session_id]
        # 清空消息缓冲区
        if session_id in self.__sessions_prompt_buffer:
            del self.__sessions_prompt_buffer[session_id]
        if session_id in self.__last_human_interaction:
            del self.__last_human_interaction[session_id]
        if session_id in self.__session_locks:
            del self.__session_locks[session_id]


copilot = CopilotSessionManager()


driver = get_driver()


@driver.on_startup
async def startup():
    await copilot._client.start()
    copilot.start_keepalive()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    await copilot.stop_keepalive()
    await copilot._client.stop()
    logger.info("Copilot客户端已关闭")
