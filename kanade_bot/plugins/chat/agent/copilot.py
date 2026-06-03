import asyncio
from collections import deque
from typing import Callable

from copilot import CopilotSession
from copilot.rpc import ModelSwitchToRequest
from copilot.session import Attachment, PermissionHandler, SystemMessageConfig
from copilot.session_events import (
    AssistantMessageData,
    AssistantMessageDeltaData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
)
from copilot.tools import Tool
from nonebot import logger

from kanade_bot.utils.common import COPILOT_CLIENT, Ptr
from kanade_bot.utils.parse import build_sender_info
from kanade_bot.utils.session import SessionInfo

from ..config import cfg
from .memory import delete_session_memory, set_memory_context
from .tool import (
    list_memes,
    read_memory,
    tavily_extract,
    tavily_search,
    view_image,
    write_memory,
)


class CopilotSessionManager:
    """Copilot会话管理器，负责管理会话对象、消息缓冲区、会话锁等资源，并提供发送消息、添加缓冲消息、重置会话等功能"""

    SESSION_COMPACTION_RETRY_MAX = 2
    """发生压缩时最多重发次数"""

    tools: list[Tool] = [
        tavily_search,
        tavily_extract,
        list_memes,
        read_memory,
        write_memory,
        view_image,
    ]

    available_tools: list[str] = [
        "view",
        "read",
        "search",
        "grep",
        "glob",
        "sql",
        "skill",
        "web_fetch",
        "web_search",
        *(tool.name for tool in tools),
    ]
    """工具列表，包含所有可用工具的名称"""

    system_prompt_path = cfg.system_prompt_file_path
    system_prompt = ""
    """系统提示词"""
    if not system_prompt_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {system_prompt_path.absolute()}")
    else:
        system_prompt = system_prompt_path.read_text(encoding="utf-8")

    system_message: SystemMessageConfig = {
        "mode": "replace",
        "content": system_prompt,
    }

    SESSION_CONFIG = {
        "on_permission_request": PermissionHandler.approve_all,
        "model": cfg.model,
        "reasoning_effort": "medium",
        "tools": tools,
        "available_tools": available_tools,
        "system_message": system_message,
        "provider": cfg.provider,
    }

    def __init__(self):
        self._sessions: dict[str, CopilotSession] = {}
        """会话对象缓存，键为会话ID，值为CopilotSession对象"""

        self._sessions_prompt_buffer: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表"""

        self._session_locks: dict[str, asyncio.Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID，值为Lock对象"""

        self._global_lock = asyncio.Lock()
        """全局资源锁，对sessions字典的修改操作加锁，对_client对象的操作加锁，确保线程安全"""

    async def _resume_or_create_session(self, session_id: str) -> tuple[CopilotSession, bool]:
        """尝试恢复会话，恢复失败则创建新会话，并确保会话配置正确，返回会话对象和是否是新会话的标志"""
        new_session = False
        try:
            session = await COPILOT_CLIENT.resume_session(session_id, **self.SESSION_CONFIG)
            logger.info(f"恢复会话{session_id}成功")
        except Exception as e:
            logger.info(f"恢复会话{session_id}失败，将创建新会话: {e}")
            session = await COPILOT_CLIENT.create_session(
                session_id=session_id, **self.SESSION_CONFIG
            )
            new_session = True

        current_model = await session.rpc.model.get_current()
        if current_model.model_id != cfg.model:
            logger.warning(
                "会话{}模型设置失败，期望{}，但实际是{}，请检查模型是否可用或名称是否正确，将设置为gpt-4.1",
                session_id,
                cfg.model,
                current_model.model_id,
            )
            await session.rpc.model.switch_to(ModelSwitchToRequest("gpt-4.1"))

        return session, new_session

    async def send(
        self,
        session_info: SessionInfo,
        prompt: str,
        handler: Callable[[SessionEvent], None],
        *,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> tuple[str, Callable[[], None]] | None:
        """发送消息到会话，自动处理会话恢复、消息缓冲区、提示词构建、事件监听等逻辑

        是对`CopilotSession.send`的封装

        :param prompt: 用户消息文本内容，如果为None，则表示没有新的用户消息，
        仅使用缓冲区中的消息和引用消息

        :returns message_id: The message ID assigned by the server,
        which can be used to correlate events.
        :returns unsubscribe: A function that, when called, unsubscribes the handler.
        """
        session_id = session_info.session_id
        async with await self._ensure_session_lock(session_id):
            async with self._global_lock:
                session = self._sessions.get(session_id)

            new_session = False
            if not session:
                session, new_session = await self._resume_or_create_session(session_id)
                async with self._global_lock:
                    self._sessions[session_id] = session

            if new_session:
                logger.info(f"会话{session_id}是新会话，旧会话可能被手动删除或损坏")
                delete_session_memory(session_id)

            async with self._global_lock:
                if new_session:
                    self._sessions_prompt_buffer[session_id] = deque(
                        maxlen=cfg.session_prompt_buffer_max_size
                    )

                # 将消息缓冲区中的消息添加到选项中
                buffered_messages = self._sessions_prompt_buffer.get(session_id)
                if not prompt and not buffered_messages and not reply_text:
                    # 没有任何新的消息可发送，直接返回
                    return None

            send_prompt = self._build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                buffered_messages=buffered_messages,
                reply_text=reply_text,
            )
            logger.debug(f"发送到会话{session_id}的完整提示词:\n{send_prompt}")

            set_memory_context(session_info)

            unsubscribe = session.on(handler)
            message_id = await session.send(
                send_prompt,
                attachments=attachments,
            )

            async with self._global_lock:
                # 清空消息缓冲区
                if session_id in self._sessions_prompt_buffer:
                    self._sessions_prompt_buffer[session_id].clear()

            return message_id, unsubscribe

    async def send_and_wait(
        self,
        session_info: SessionInfo,
        prompt: str,
        *,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> str | None:

        idle_event = asyncio.Event()
        error_event: Exception | None = None
        last_assistant_message: str | None = None

        def handler(event: SessionEvent) -> None:
            nonlocal last_assistant_message, error_event
            match event.data:
                case AssistantMessageData() as data:
                    last_assistant_message = data.content
                case SessionIdleData():
                    idle_event.set()
                case SessionErrorData() as data:
                    error_event = Exception(f"Session error: {data.message or str(data)}")
                    idle_event.set()

        unsubscribe: Callable[[], None] | None = None
        try:
            t = await self.send(
                session_info,
                prompt,
                handler,
                rag_docs=rag_docs,
                reply_text=reply_text,
                attachments=attachments,
            )
            if not t:
                # 发送的消息为空
                logger.info("发送给模型的消息为空，未触发生成")
                return

            _, unsubscribe = t

            await asyncio.wait_for(idle_event.wait(), timeout=timeout)

            if error_event:
                raise error_event

            return last_assistant_message
        except TimeoutError:
            raise
        finally:
            if unsubscribe:
                unsubscribe()

    async def send_and_stream(
        self,
        session_info: SessionInfo,
        prompt: str,
        stream_queue: asyncio.Queue[str | None],
        stream_error_ptr: Ptr[Exception | None],
        *,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> Callable[[], None] | None:
        """发送消息并通过stream_queue实时返回生成内容，直到生成完成或发生错误

        :param stream_queue: 用于接收生成内容的异步队列，生成的每个文本块都会通过
        `stream_queue.put_nowait`发送到队列中。当生成完成时，发送一个None表示结束。
        :param stream_error_ptr: 用于接收生成过程中发生的错误的变量，如果发生错误，
        设置为Exception对象，并通过`stream_queue.put_nowait(None)`发送一个None表示结束。

        :returns unsubscribe: A function that, when called, unsubscribes the handler.
        """

        def handler(event: SessionEvent) -> None:
            nonlocal stream_error_ptr
            match event.data:
                case AssistantMessageDeltaData() as delta:
                    stream_queue.put_nowait(delta.delta_content)
                case SessionIdleData():
                    stream_queue.put_nowait(None)
                case SessionErrorData() as data:
                    stream_error_ptr.v = Exception(f"Session error: {data.message or str(data)}")
                    stream_queue.put_nowait(None)

        t = await self.send(
            session_info,
            prompt,
            handler,
            rag_docs=rag_docs,
            reply_text=reply_text,
            attachments=attachments,
        )

        if not t:
            # 发送的消息为空
            logger.info("发送给模型的消息为空，未触发生成")
            return

        _, unsubscribe = t
        return unsubscribe

    @staticmethod
    def _build_send_prompt(
        session_info: SessionInfo,
        prompt: str,
        *,
        rag_docs: list[str] | None = None,
        buffered_messages: deque[str] | None = None,
        reply_text: str | None = None,
    ) -> str:
        """构建发送给模型的完整提示词"""
        prompt_parts: list[str] = []

        if group_info := build_sender_info(session_info.group_name, session_info.group_id):
            prompt_parts.append(f"$现在的会话在群聊{group_info}中。")

        if rag_docs:
            prompt_parts.append("$检索到可能相关的文档：")
            prompt_parts.extend(rag_docs)
        if buffered_messages:
            prompt_parts.append("$下面是之前的消息缓冲区中的消息：")
            prompt_parts.extend(buffered_messages)
        if reply_text:
            prompt_parts.append("$用户引用了之前的消息：")
            prompt_parts.append(reply_text)

        if user_info := build_sender_info(session_info.nickname, session_info.user_id):
            prompt = f"{user_info} : {prompt}"
        if prompt:
            prompt_parts.append("$下面是这次的用户消息：")
            prompt_parts.append(prompt)

        return "\n".join(prompt_parts).strip()

    async def _ensure_session_lock(self, session_id: str) -> asyncio.Lock:
        """确保会话锁存在并返回"""
        # 不要在持有全局锁的情况下调用此函数，以避免死锁
        if session_id not in self._session_locks:
            # 略微提高性能，避免不必要的锁竞争
            async with self._global_lock:
                if session_id not in self._session_locks:
                    self._session_locks[session_id] = asyncio.Lock()
        return self._session_locks[session_id]

    def get_session_prompt_buffer_size(self, session_id: str) -> int:
        """获取会话消息缓冲区大小"""
        return len(self._sessions_prompt_buffer.get(session_id, []))

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        async with await self._ensure_session_lock(session_id):
            async with self._global_lock:
                if session_id not in self._sessions_prompt_buffer:
                    self._sessions_prompt_buffer[session_id] = deque(
                        maxlen=cfg.session_prompt_buffer_max_size
                    )
                # deque(maxlen)会在溢出时自动丢弃最早的消息
                self._sessions_prompt_buffer[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """删除会话，清空缓冲区。**此操作不可逆**"""
        # 先获取会话锁，确保同一时间只有一个协程在操作同一个会话。
        # 全局锁只保护字典访问，不包住耗时的客户端 RPC。
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                session = self._sessions.pop(session_id, None)
                if session_id in self._sessions_prompt_buffer:
                    del self._sessions_prompt_buffer[session_id]

            # 断开并删除现有会话
            try:
                if session:
                    await session.disconnect()
                await COPILOT_CLIENT.delete_session(session_id)
            except RuntimeError as e:
                logger.warning(f"删除会话{session_id}时发生错误: {e}")
            delete_session_memory(session_id)


copilot = CopilotSessionManager()
