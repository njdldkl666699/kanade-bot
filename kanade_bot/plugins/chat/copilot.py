from asyncio import Lock
from collections import deque
from pathlib import Path

from copilot import CopilotClient, CopilotSession
from copilot.client import StopError
from copilot.generated.rpc import ModelSwitchToRequest
from copilot.generated.session_events import AssistantMessageData, SessionEvent, SessionEventType
from copilot.session import Attachment, PermissionHandler, SystemMessageConfig
from copilot.tools import Tool
from nonebot import get_driver, logger

from ..util import SessionInfo, build_sender_info
from .config import cfg
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
        "grep",
        "glob",
        "sql",
        "skill",
        "web_fetch",
        *(tool.name for tool in tools),
    ]
    """工具列表，包含所有可用工具的名称"""

    system_prompt_path = Path(cfg.chat_system_prompt_file_path)
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
        "model": cfg.chat_model,
        "reasoning_effort": "medium",
        "tools": tools,
        "available_tools": available_tools,
        "system_message": system_message,
    }

    def __init__(self):
        self._client = CopilotClient()
        """Copilot客户端对象，负责与Copilot服务进行通信，创建和恢复会话等操作"""

        self.__sessions: dict[str, CopilotSession] = {}
        """会话对象缓存，键为会话ID，值为CopilotSession对象"""

        self.__sessions_prompt_buffer: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表"""

        self.__session_locks: dict[str, Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID，值为Lock对象"""

        self.__global_lock = Lock()
        """全局资源锁，对sessions字典的修改操作加锁，对_client对象的操作加锁，确保线程安全"""

    async def _resume_or_create_session(self, session_id: str) -> tuple[CopilotSession, bool]:
        """尝试恢复会话，恢复失败则创建新会话，并确保会话配置正确，返回会话对象和是否是新会话的标志"""
        new_session = False
        try:
            session = await self._client.resume_session(session_id, **self.SESSION_CONFIG)
            logger.info(f"恢复会话{session_id}成功")
        except Exception as e:
            logger.info(f"恢复会话{session_id}失败，将创建新会话: {e}")
            session = await self._client.create_session(
                session_id=session_id, **self.SESSION_CONFIG
            )
            new_session = True

        current_model = await session.rpc.model.get_current()
        if current_model.model_id != cfg.chat_model:
            logger.warning(
                "会话{}模型设置失败，期望{}，但实际是{}，请检查模型是否可用或名称是否正确，将设置为gpt-4.1",
                session_id,
                cfg.chat_model,
                current_model.model_id,
            )
            await session.rpc.model.switch_to(ModelSwitchToRequest("gpt-4.1"))

        return session, new_session

    async def send_and_wait(
        self,
        session_info: SessionInfo,
        prompt: str,
        *,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> tuple[SessionEvent | None, bool]:
        """发送消息并等待响应，返回响应事件和是否是新会话

        prompt: 用户消息文本内容，如果为None，则表示没有新的用户消息，
        仅使用缓冲区中的消息和引用消息
        """
        session_id = session_info.session_id
        async with await self._ensure_session_lock(session_id):
            async with self.__global_lock:
                session = self.__sessions.get(session_id)

            new_session = False
            if not session:
                session, new_session = await self._resume_or_create_session(session_id)
                async with self.__global_lock:
                    self.__sessions[session_id] = session

            if new_session:
                delete_session_memory(session_id)

            async with self.__global_lock:
                if new_session:
                    self.__sessions_prompt_buffer[session_id] = deque(
                        maxlen=cfg.chat_session_prompt_buffer_max_size
                    )

                # 将消息缓冲区中的消息添加到选项中
                buffered_messages = self.__sessions_prompt_buffer.get(session_id)
                if not prompt and not buffered_messages and not reply_text:
                    # 没有任何新的消息可发送，直接返回
                    return None, new_session

                # 清空消息缓冲区
                if session_id in self.__sessions_prompt_buffer:
                    self.__sessions_prompt_buffer[session_id].clear()

            send_prompt = self._build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                buffered_messages=buffered_messages,
                reply_text=reply_text,
            )

            session_event: SessionEvent | None = None
            try:
                set_memory_context(session_info)
                # session_event = await self._send_with_compaction_retry(
                #     session,
                #     session_id,
                #     prompt=send_prompt,
                #     attachments=attachments,
                #     timeout=timeout,
                # )
                # 需要校验测试，在新版SDK下会不会把压缩的结果也返回
                session_event = await session.send_and_wait(
                    send_prompt,
                    attachments=attachments,
                    timeout=timeout,
                )
            except Exception as e:
                logger.warning(f"发送消息或等待响应时发生错误: {e}")

        return session_event, new_session

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

    @classmethod
    async def _send_with_compaction_retry(
        cls,
        session: CopilotSession,
        session_id: str,
        *,
        prompt: str,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> SessionEvent | None:
        """发送消息并在压缩发生时拒收本次响应，自动重发当前消息。"""
        for attempt in range(cls.SESSION_COMPACTION_RETRY_MAX + 1):
            compaction_started = False

            def on_session_compaction_start(event: SessionEvent):
                nonlocal compaction_started
                if event.type == SessionEventType.SESSION_COMPACTION_START:
                    compaction_started = True
                    logger.warning(f"会话{session_id}开始压缩，本轮响应将被丢弃")

            unsubscribe = session.on(on_session_compaction_start)
            try:
                session_event = await session.send_and_wait(
                    prompt,
                    attachments=attachments,
                    timeout=timeout,
                )
            finally:
                unsubscribe()

            if not compaction_started:
                return session_event
            else:
                #### DEBUG ####
                logger.warning(f"会话{session_id}发生压缩，已丢弃本次响应")
                if session_event:
                    match session_event.data:
                        case AssistantMessageData() as data:
                            logger.info(f"会话{session_id}压缩后的响应内容: {data.content}")

            if attempt >= cls.SESSION_COMPACTION_RETRY_MAX:
                logger.warning(
                    f"会话{session_id}在压缩后重试超过上限{cls.SESSION_COMPACTION_RETRY_MAX}次，返回最后一次响应"
                )
                return session_event

            logger.info(f"会话{session_id}压缩后将重发当前消息，正在进行第{attempt + 1}次重试")

        return None

    async def _ensure_session_lock(self, session_id: str) -> Lock:
        """确保会话锁存在并返回"""
        # 不要在持有全局锁的情况下调用此函数，以避免死锁
        if session_id not in self.__session_locks:
            # 略微提高性能，避免不必要的锁竞争
            async with self.__global_lock:
                if session_id not in self.__session_locks:
                    self.__session_locks[session_id] = Lock()
        return self.__session_locks[session_id]

    def get_session_prompt_buffer_size(self, session_id: str) -> int:
        """获取会话消息缓冲区大小"""
        return len(self.__sessions_prompt_buffer.get(session_id, []))

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        async with await self._ensure_session_lock(session_id):
            async with self.__global_lock:
                if session_id not in self.__sessions_prompt_buffer:
                    self.__sessions_prompt_buffer[session_id] = deque(
                        maxlen=cfg.chat_session_prompt_buffer_max_size
                    )
                # deque(maxlen)会在溢出时自动丢弃最早的消息
                self.__sessions_prompt_buffer[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """删除会话，清空缓冲区。**此操作不可逆**"""
        # 先获取会话锁，确保同一时间只有一个协程在操作同一个会话。
        # 全局锁只保护字典访问，不包住耗时的客户端 RPC。
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self.__global_lock:
                session = self.__sessions.pop(session_id, None)
                if session_id in self.__sessions_prompt_buffer:
                    del self.__sessions_prompt_buffer[session_id]

            # 断开并删除现有会话
            try:
                if session:
                    await session.disconnect()
                await self._client.delete_session(session_id)
            except RuntimeError as e:
                logger.warning(f"删除会话{session_id}时发生错误: {e}")
            delete_session_memory(session_id)


copilot = CopilotSessionManager()


driver = get_driver()


@driver.on_startup
async def startup():
    await copilot._client.start()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    try:
        await copilot._client.stop()
    except* StopError as eg:
        logger.warning(f"停止Copilot客户端时发生错误: {eg.message}")
    logger.info("Copilot客户端已关闭")
