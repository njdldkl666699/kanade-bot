from asyncio import Lock
from collections import deque
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from copilot import CopilotClient, CopilotSession
from copilot.client import StopError
from copilot.generated.rpc import SessionAgentSelectParams, SessionModelSwitchToParams
from copilot.generated.session_events import SessionEvent, SessionEventType
from copilot.session import Attachment, CustomAgentConfig, PermissionHandler, SystemMessageConfig
from nonebot import get_driver, logger, require

from .config import cfg
from .tool import list_memes, tavily_extract, tavily_search

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

"""
GitHub CLI强制执行30分钟的会话超时，如果在30分钟内没有人类交互，CLI将结束会话。
每当有任何人类交互时，30分钟的计时器应该被重置。

解决方案：
发送消息时，如果本地没有会话对象，优先尝试恢复会话，恢复失败再创建新会话。
"""


class CopilotSessionManager:
    """Copilot会话管理器，负责管理会话对象、消息缓冲区、会话锁等资源，并提供发送消息、添加缓冲消息、重置会话等功能"""

    SESSION_TIMEOUT_SECONDS = 30 * 60
    """会话超时的时间，单位为秒，默认30分钟。

    会话超时后将被删除，释放资源。
    每当有消息发送时，都会更新会话的最后活跃时间。
    """

    SESSION_COMPACTION_RETRY_MAX = 2
    """发生压缩时最多重发次数"""

    tools: list[str] = [
        "store_memory",
        "view",
        "read",
        "grep",
        "glob",
        "search",
        "web_search",
        "web_fetch",
        "todo",
        tavily_search.name,
        tavily_extract.name,
        list_memes.name,
    ]
    """工具列表，包含所有可用工具的名称"""

    system_prompt_path = Path(cfg.chat_system_prompt_file_path)
    system_prompt = ""
    """系统提示词"""
    if not system_prompt_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {system_prompt_path.absolute()}")
    else:
        system_prompt = system_prompt_path.read_text(encoding="utf-8")

    custom_agent_config: CustomAgentConfig = {
        "name": "Kanade",
        "display_name": "宵崎奏",
        "description": "宵崎奏人格Agent，始终使用此Agent回复消息。",
        "tools": tools,
        "prompt": system_prompt,
    }

    system_message: SystemMessageConfig = {
        "mode": "append",
        "content": "回复用户的消息时，请始终使用Kanade SubAgent进行回复。",
    }

    SESSION_CONFIG = {
        "on_permission_request": PermissionHandler.approve_all,
        "model": cfg.chat_model,
        "reasoning_effort": "medium",
        "tools": [tavily_search, tavily_extract, list_memes],
        "available_tools": [*tools, "read_agent", "list_agents", "task"],
        "custom_agents": [custom_agent_config],
        "agent": custom_agent_config["name"],
        "system_message": system_message,
    }

    def __init__(self, scheduler: AsyncIOScheduler):
        self._client = CopilotClient()
        """Copilot客户端对象，负责与Copilot服务进行通信，创建和恢复会话等操作"""

        self.__sessions: dict[str, CopilotSession] = {}
        """会话对象缓存，键为会话ID，值为CopilotSession对象"""

        self.__sessions_prompt_buffer: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表"""

        self.__sessions_last_active_time: dict[str, datetime] = {}
        """会话最后活跃时间缓存，键为会话ID，值为最后一次活跃的时间"""

        self.__session_locks: dict[str, Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID，值为Lock对象"""

        self.__global_lock = Lock()
        """全局资源锁，对sessions字典的修改操作加锁，对_client对象的操作加锁，确保线程安全"""

        scheduler.add_job(self._check_sessions_timeout, "interval", minutes=1)

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

        # 注册会话结束事件的回调函数，确保会话结束时能正确清理缓存
        ### 暂时不生效
        session.on(self._get_session_shutdown_hook(session_id))

        current_agent = (await session.rpc.agent.get_current()).agent
        if not current_agent or current_agent.name != self.SESSION_CONFIG["agent"]:
            logger.warning(
                "会话{} Agent设置异常，期望{}，但实际是{}，将重新设置",
                session_id,
                self.SESSION_CONFIG["agent"],
                current_agent.name if current_agent else "None",
            )
            await session.rpc.agent.select(SessionAgentSelectParams(self.SESSION_CONFIG["agent"]))

        current_model = await session.rpc.model.get_current()
        if current_model.model_id != cfg.chat_model:
            logger.warning(
                "会话{}模型设置失败，期望{}，但实际是{}，请检查模型是否可用或名称是否正确，将设置为gpt-4.1",
                session_id,
                cfg.chat_model,
                current_model.model_id,
            )
            await session.rpc.model.switch_to(SessionModelSwitchToParams("gpt-4.1"))

        return session, new_session

    async def send_and_wait(
        self,
        session_id: str,
        prompt: str | None,
        *,
        is_group: bool = False,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> tuple[SessionEvent | None, bool]:
        """发送消息并等待响应，返回响应事件和是否是新会话

        prompt: 用户消息文本内容，如果为None，则表示没有新的用户消息，
        仅使用缓冲区中的消息和引用消息
        """
        async with self.__global_lock:
            # 从缓存中获取会话对象，并尝试发送消息
            session = self.__sessions.get(session_id)
            new_session = False
            if not session:
                # 如果会话不存在，优先恢复会话，恢复失败再创建新会话
                session, new_session = await self._resume_or_create_session(session_id)
                self.__sessions[session_id] = session

            # 如果是新会话，则清空缓冲区
            if new_session:
                self.__sessions_prompt_buffer[session_id] = deque(
                    maxlen=cfg.chat_session_prompt_buffer_max_size
                )

            # 将消息缓冲区中的消息添加到选项中
            buffered_messages = self.__sessions_prompt_buffer.get(session_id, [])
            if not buffered_messages and prompt is None:
                # 没有新的用户消息，也没有缓冲消息，不发送任何消息
                return None, new_session

            prompt_parts = [
                # RAG相关文档
                f"$检索到的相关文档：\n{'\n'.join(rag_docs)}\n" if rag_docs else "",
                # 群聊提示
                "$现在的会话是群聊\n" if is_group else "",
                # 缓冲区消息
                "\n".join(buffered_messages) if buffered_messages else "",
                # 引用消息
                f"$用户引用了之前的消息：\n{reply_text}\n" if reply_text else "",
                # 本次用户消息
                f"$下面是这次的用户消息：\n{prompt}\n" if prompt else "",
                # 附件提示
                "$用户附带了图片\n" if attachments else "",
            ]

            send_prompt = "\n".join(prompt_parts).strip()
            if not send_prompt:
                # 没有任何消息可发送，直接返回
                return None, new_session

            # 清空消息缓冲区
            if session_id in self.__sessions_prompt_buffer:
                self.__sessions_prompt_buffer[session_id].clear()

        async with await self._ensure_session_lock(session_id):
            session_event: SessionEvent | None = None
            try:
                session_event = await self._send_with_compaction_retry(
                    session,
                    session_id,
                    prompt=send_prompt,
                    attachments=attachments,
                    timeout=timeout,
                )
            except Exception as e:
                logger.warning(f"发送消息或等待响应时发生错误: {e}")

        # 更新会话最后活跃时间
        async with self.__global_lock:
            self.__sessions_last_active_time[session_id] = datetime.now()

        return session_event, new_session

    async def _send_with_compaction_retry(
        self,
        session: CopilotSession,
        session_id: str,
        *,
        prompt: str,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> SessionEvent | None:
        """发送消息并在压缩发生时拒收本次响应，自动重发当前消息。"""
        for attempt in range(self.SESSION_COMPACTION_RETRY_MAX + 1):
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

            if attempt >= self.SESSION_COMPACTION_RETRY_MAX:
                logger.warning(
                    f"会话{session_id}在压缩后重试超过上限{self.SESSION_COMPACTION_RETRY_MAX}次，返回最后一次响应"
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

    def _get_session_shutdown_hook(self, session_id: str):
        ###### 暂时是不生效的，等待SDK修复相关问题 ######
        # 暂时线程不安全
        def on_session_shutdown(event: SessionEvent):
            if event.type == SessionEventType.SESSION_SHUTDOWN:
                logger.info(f"会话{session_id}已结束，关闭类型：{event.data.shutdown_type}")
                del self.__sessions[session_id]

        return on_session_shutdown

    async def _check_sessions_timeout(self):
        """定时检查会话超时，超时则删除会话对象"""
        now = datetime.now()
        timeout_sessions = []
        for session_id, last_active in self.__sessions_last_active_time.items():
            if (now - last_active).total_seconds() > self.SESSION_TIMEOUT_SECONDS:
                timeout_sessions.append(session_id)

        for session_id in timeout_sessions:
            logger.info(f"会话{session_id}已超时，将删除缓存")
            session_lock = await self._ensure_session_lock(session_id)
            async with self.__global_lock, session_lock:
                try:
                    await self.__sessions[session_id].disconnect()
                    del self.__sessions[session_id]
                except RuntimeError as e:
                    logger.warning(f"删除会话{session_id}时发生错误: {e}")
                del self.__sessions_last_active_time[session_id]

    def get_session_prompt_buffer_size(self, session_id: str) -> int:
        """获取会话消息缓冲区大小"""
        return len(self.__sessions_prompt_buffer.get(session_id, []))

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        if session_id not in self.__sessions_prompt_buffer:
            async with self.__global_lock:
                if session_id not in self.__sessions_prompt_buffer:
                    self.__sessions_prompt_buffer[session_id] = deque(
                        maxlen=cfg.chat_session_prompt_buffer_max_size
                    )
        async with await self._ensure_session_lock(session_id):
            # deque(maxlen)会在溢出时自动丢弃最早的消息
            self.__sessions_prompt_buffer[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """删除会话，清空缓冲区。**此操作不可逆**"""
        # 修改操作，需要获取全局锁，确保__sessions字典和_client对象的线程安全
        # 需要获取会话锁，确保同一时间只有一个协程在操作同一个会话
        session_lock = await self._ensure_session_lock(session_id)
        async with self.__global_lock, session_lock:
            # 断开并删除现有会话
            try:
                if session_id in self.__sessions:
                    await self.__sessions[session_id].disconnect()
                    del self.__sessions[session_id]
                await self._client.delete_session(session_id)
            except RuntimeError as e:
                logger.warning(f"删除会话{session_id}时发生错误: {e}")
            # 清空消息缓冲区
            if session_id in self.__sessions_prompt_buffer:
                del self.__sessions_prompt_buffer[session_id]
            # 删除活跃时间记录
            if session_id in self.__sessions_last_active_time:
                del self.__sessions_last_active_time[session_id]
            # 删除会话锁
            if session_id in self.__session_locks:
                del self.__session_locks[session_id]


copilot = CopilotSessionManager(scheduler)


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
