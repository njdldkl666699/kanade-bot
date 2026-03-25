from asyncio import Lock
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from copilot import (
    Attachment,
    CopilotClient,
    CopilotSession,
    CustomAgentConfig,
    PermissionHandler,
    SessionEvent,
)
from copilot.generated.rpc import SessionAgentSelectParams, SessionModelSwitchToParams
from copilot.generated.session_events import SessionEventType
from loguru import logger
from nonebot import get_driver, get_plugin_config, require

from kanade_bot.plugins.chat.tool import tavily_search

require("nonebot_plugin_apscheduler")

from nonebot_plugin_apscheduler import scheduler

from .config import Config

cfg = get_plugin_config(Config)

"""
GitHub CLI强制执行30分钟的会话超时，如果在30分钟内没有人类交互，CLI将结束会话。
每当有任何人类交互时，30分钟的计时器应该被重置。

解决方案：
发送消息时，如果本地没有会话对象，优先尝试恢复会话，恢复失败再创建新会话。
"""


class CopilotSessionManager:
    SESSION_TIMEOUT_SECONDS = 30 * 60  # 30分钟会话超时

    def __init__(self, scheduler: AsyncIOScheduler):
        self._client = CopilotClient()
        self.__tools: list[str] = [
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
        ]

        system_prompt_path = Path(cfg.chat_system_prompt_path)
        system_prompt = ""
        if not system_prompt_path.is_file():
            logger.warning(f"系统提示词文件不存在，路径: {system_prompt_path.absolute()}")
        else:
            system_prompt = system_prompt_path.read_text(encoding="utf-8")

        self.__custom_agent_config: CustomAgentConfig = {
            "name": "Kanade",
            "display_name": "宵崎奏",
            "description": "宵崎奏人格Agent，始终使用此Agent回复消息。",
            "tools": self.__tools,
            "prompt": system_prompt,
        }

        self.__session_config = {
            "on_permission_request": PermissionHandler.approve_all,
            "model": cfg.chat_model,
            "tools": [tavily_search],
            "available_tools": self.__tools,
            "custom_agents": [self.__custom_agent_config],
            "agent": self.__custom_agent_config["name"],  # pyright: ignore[reportTypedDictNotRequiredAccess]
        }

        # 会话对象缓存，键为会话ID，值为CopilotSession对象
        self.__sessions: dict[str, CopilotSession] = {}
        # 会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表
        self.__sessions_prompt_buffer: dict[str, list[str]] = {}

        # 会话最后活跃时间缓存，键为会话ID，值为最后一次活跃的时间
        self.__sesssions_last_active_time: dict[str, datetime] = {}
        # 会话锁，确保同一时间只有一个协程在操作sessions
        self.__sessions_lock = Lock()

        scheduler.add_job(self.check_sessions_timeout, "interval", minutes=1)

    async def _resume_or_create_session(self, session_id: str) -> tuple[CopilotSession, bool]:
        """尝试恢复会话，恢复失败则创建新会话，并确保会话配置正确，返回会话对象和是否是新会话的标志"""
        new_session = False
        try:
            session = await self._client.resume_session(session_id, **self.__session_config)
            logger.info(f"恢复会话{session_id}成功")
        except Exception as e:
            logger.info(f"恢复会话{session_id}失败，将创建新会话: {e}")
            session = await self._client.create_session(
                session_id=session_id, **self.__session_config
            )
            new_session = True

        # 注册会话结束事件的回调函数，确保会话结束时能正确清理缓存
        ### 暂时不生效
        session.on(self._get_session_shutdown_hook(session_id))

        current_agent = (await session.rpc.agent.get_current()).agent
        if not current_agent or current_agent.name != self.__session_config["agent"]:
            logger.warning(
                "会话{} Agent设置异常，期望{}，但实际是{}，将重新设置",
                session_id,
                self.__session_config["agent"],
                current_agent.name if current_agent else "None",
            )
            await session.rpc.agent.select(SessionAgentSelectParams(self.__session_config["agent"]))

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
        prompt: str,
        *,
        is_group: bool = False,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
        timeout: float = 60,
    ) -> tuple[SessionEvent | None, bool]:
        """发送消息并等待响应，返回响应事件和是否是新会话"""
        async with self.__sessions_lock:
            # 从缓存中获取会话对象，并尝试发送消息
            session = self.__sessions.get(session_id)
            new_session = False
            if not session:
                # 如果会话不存在，优先恢复会话，恢复失败再创建新会话
                session, new_session = await self._resume_or_create_session(session_id)
                self.__sessions[session_id] = session

            # 如果是新会话，则清空缓冲区
            if new_session:
                self.__sessions_prompt_buffer[session_id] = []

            # 将消息缓冲区中的消息添加到选项中
            buffered_messages = self.__sessions_prompt_buffer.get(session_id, [])
            if buffered_messages:
                prompt = (
                    ("$现在的会话是群聊\n" if is_group else "")
                    + f"$下面是上次对话和这次对话之间的消息：\n{'\n'.join(buffered_messages)}\n\n"
                    + (f"$用户引用了之前的消息：\n{reply_text}\n\n" if reply_text else "")
                    + f"$下面是这次的用户消息：\n{prompt}\n\n"
                    + ("$用户附带了图片" if attachments else "")
                )
                # 清空消息缓冲区
                self.__sessions_prompt_buffer[session_id] = []

            session_event: SessionEvent | None = None
            try:
                session_event = await session.send_and_wait(
                    prompt, attachments=attachments, timeout=timeout
                )
                self.update_session_active_time(session_id)
            except Exception as e:
                logger.warning(f"发送消息或等待响应时发生错误: {e}")

            return session_event, new_session

    def _get_session_shutdown_hook(self, session_id: str):
        ###### 暂时是不生效的，等待SDK修复相关问题 ######
        def on_session_shutdown(event: SessionEvent):
            if event.type == SessionEventType.SESSION_SHUTDOWN:
                logger.info(f"会话{session_id}已结束，关闭类型：{event.data.shutdown_type}")
                del self.__sessions[session_id]

        return on_session_shutdown

    async def check_sessions_timeout(self):
        """定时检查会话超时，超时则删除会话对象"""
        async with self.__sessions_lock:
            now = datetime.now()
            timeout_sessions = []
            for session_id, last_active in self.__sesssions_last_active_time.items():
                if (now - last_active).total_seconds() > self.SESSION_TIMEOUT_SECONDS:
                    timeout_sessions.append(session_id)

            for session_id in timeout_sessions:
                logger.info(f"会话{session_id}已超时，将删除缓存")
                try:
                    await self.__sessions[session_id].disconnect()
                    del self.__sessions[session_id]
                except RuntimeError as e:
                    logger.warning(f"删除会话{session_id}时发生错误: {e}")
                del self.__sesssions_last_active_time[session_id]

    def update_session_active_time(self, session_id: str):
        """更新会话最后活跃时间"""
        self.__sesssions_last_active_time[session_id] = datetime.now()

    def get_session_prompt_buffer_size(self, session_id: str) -> int:
        """获取会话消息缓冲区大小"""
        if session_id not in self.__sessions_prompt_buffer:
            self.__sessions_prompt_buffer[session_id] = []
        return len(self.__sessions_prompt_buffer[session_id])

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        if session_id not in self.__sessions_prompt_buffer:
            self.__sessions_prompt_buffer[session_id] = []
        self.__sessions_prompt_buffer[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """删除会话，清空缓冲区。**此操作不可逆**"""
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
        if session_id in self.__sesssions_last_active_time:
            del self.__sesssions_last_active_time[session_id]


copilot = CopilotSessionManager(scheduler)


driver = get_driver()


@driver.on_startup
async def startup():
    await copilot._client.start()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    await copilot._client.stop()
    logger.info("Copilot客户端已关闭")
