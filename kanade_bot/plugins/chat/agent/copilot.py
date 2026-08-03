import asyncio
import json
from collections import deque
from typing import Any

from copilot import CopilotSession
from copilot.session import Attachment, PermissionHandler
from copilot.session_events import AssistantMessageData
from nonebot import get_driver, logger

from kanade_bot.utils.common import COPILOT_CLIENT
from kanade_bot.utils.parse import build_sender_info
from kanade_bot.utils.session import SessionInfo

from ..config import cfg
from .memory import MemoryContext, MemoryStore
from .tool import build_memory_tools, get_image_caption, list_memes, view_image

FALLBACK_SYSTEM_PROMPT = "你是一只可爱的猫娘。"


def _build_system_prompt() -> str:
    sp_path = cfg.system_prompt_file_path
    if not sp_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {sp_path.absolute()}")
        return FALLBACK_SYSTEM_PROMPT

    sp = sp_path.read_text(encoding="utf-8")
    extras = cfg.system_prompt_extras_paths

    for k, p in extras.items():
        if not p.is_file():
            logger.warning(f"系统提示词额外内容文件不存在，路径: {p.absolute()}")
            continue
        content = p.read_text(encoding="utf-8")
        sp = sp.replace(f"{{{{{k}}}}}", content)

    return sp


class CopilotSessionManager:
    """Copilot会话管理器，负责管理会话对象、消息缓冲区、会话锁等资源，并提供发送消息、添加缓冲消息、重置会话等功能"""

    system_prompt = _build_system_prompt()
    """系统提示词"""
    logger.trace(f"系统提示词:\n{system_prompt}")

    def session_config(self, session_info: SessionInfo) -> dict[str, Any]:
        """返回会话配置字典"""
        session_system_prompt = self.system_prompt
        if group_info := build_sender_info(session_info.group_name, session_info.group_id):
            session_system_prompt += f"\n$ 现在的会话在群聊{group_info}中。"

        tools = [list_memes, view_image]
        memory_context = self._update_memory_context(session_info)
        memory_tools = build_memory_tools(memory_context, self._memory_store)
        if memory_tools:
            tools.extend(memory_tools)

        return {
            "on_permission_request": PermissionHandler.approve_all,
            "model": cfg.model,
            "provider": cfg.provider,
            "reasoning_effort": cfg.reasoning_effort,
            "tools": tools,
            "available_tools": [t.name for t in tools] + cfg.available_tools,
            "system_message": {
                "mode": "replace",
                "content": session_system_prompt,
            },
            "mcp_servers": cfg.mcp_servers,
        }

    def __init__(self):
        self._memory_store = MemoryStore(
            cfg.memory_database_file_path,
            max_memories_per_scope=cfg.memory_max_records_per_scope,
        )
        self._memory_contexts: dict[str, MemoryContext] = {}

        self._sessions: dict[str, CopilotSession] = {}
        """会话对象缓存，键为会话ID，值为CopilotSession对象"""

        self._sessions_messages: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表"""

        self._session_locks: dict[str, asyncio.Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID，值为Lock对象"""

        self._global_lock = asyncio.Lock()
        """全局资源锁，对sessions字典的修改操作加锁，对_client对象的操作加锁，确保线程安全"""

        driver = get_driver()
        driver.on_startup(self._load_sessions_messages_cache)
        driver.on_shutdown(self._save_sessions_messages_cache)

    def _load_sessions_messages_cache(self):
        """加载会话消息缓冲区缓存"""
        cache_file = cfg.session_messages_cache_file_path
        if not cache_file.is_file():
            logger.info(f"会话消息缓冲区缓存文件不存在，路径: {cache_file.absolute()}")
            return

        try:
            with cache_file.open("r", encoding="utf-8") as f:
                data: dict[str, list[str]] = json.load(f)
        except Exception as e:  # noqa: BLE001
            logger.exception(f"加载会话消息缓冲区缓存时发生错误: {e}")
            return

        for session_id, messages in data.items():
            self._sessions_messages[session_id] = deque(
                messages, maxlen=cfg.session_messages_max_size
            )
        logger.info(f"已加载{len(self._sessions_messages)}个会话的消息缓冲区缓存")

    def _save_sessions_messages_cache(self):
        """保存会话消息缓冲区缓存"""
        cache_file = cfg.session_messages_cache_file_path
        cache_file.parent.mkdir(parents=True, exist_ok=True)

        data = {id: list(m) for id, m in self._sessions_messages.items()}
        try:
            with cache_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            logger.info(f"已保存{len(self._sessions_messages)}个会话的消息缓冲区缓存")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"保存会话消息缓冲区缓存时发生错误: {e}")

    async def _resume_or_create_session(
        self,
        session_id: str,
        session_info: SessionInfo,
    ) -> tuple[CopilotSession, bool]:
        """尝试恢复会话，恢复失败则创建新会话，并确保会话配置正确，返回会话对象和是否是新会话的标志"""
        session_config = self.session_config(session_info)
        new_session = False
        try:
            session = await COPILOT_CLIENT.resume_session(session_id, **session_config)
            logger.info(f"恢复会话{session_id}成功")
        except Exception as e:  # noqa: BLE001
            logger.info(f"恢复会话{session_id}失败，将创建新会话: {e}")
            session = await COPILOT_CLIENT.create_session(session_id=session_id, **session_config)
            new_session = True
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
    ) -> str | None:
        """发送消息到会话并等待响应。

        prompt: 用户消息文本内容，如果为空，则仅使用缓冲区中的消息和引用消息。
        """
        session_id = session_info.session_id
        async with await self._ensure_session_lock(session_id):
            # Group sessions are shared by members; switch the tool context to
            # the current sender while the per-session lock is held.
            self._update_memory_context(session_info)

            async with self._global_lock:
                session = self._sessions.get(session_id)

            new_session = False
            if not session:
                session, new_session = await self._resume_or_create_session(
                    session_id, session_info
                )
                async with self._global_lock:
                    self._sessions[session_id] = session

            if new_session:
                logger.info(f"会话{session_id}是新会话，旧会话可能被手动删除或损坏")

            async with self._global_lock:
                if new_session:
                    self._sessions_messages[session_id] = deque(
                        maxlen=cfg.session_messages_max_size
                    )

                # 将消息缓冲区中的消息添加到选项中
                messages = self._sessions_messages.get(session_id)
                if not prompt and not messages and not reply_text:
                    # 没有任何新的消息可发送，直接返回
                    logger.info("发送给模型的消息为空，未触发生成")
                    return None

            send_prompt = await self._build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                messages=messages,
                reply_text=reply_text,
                attachments=attachments,
            )
            logger.debug(f"发送到会话{session_id}的完整提示词:\n{send_prompt}")

            try:
                session_event = await session.send_and_wait(
                    send_prompt,
                    attachments=attachments if cfg.image_caption_model is None else None,
                    timeout=timeout,
                )
            finally:
                async with self._global_lock:
                    # 清空消息缓冲区
                    if session_id in self._sessions_messages:
                        self._sessions_messages[session_id].clear()

            if not session_event:
                return None

            match session_event.data:
                case AssistantMessageData() as data:
                    return data.content

    @staticmethod
    async def _build_send_prompt(
        session_info: SessionInfo,
        prompt: str,
        *,
        rag_docs: list[str] | None = None,
        messages: deque[str] | None = None,
        reply_text: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> str:
        """构建发送给模型的完整提示词"""
        prompt_parts: list[str] = []

        if rag_docs:
            prompt_parts.append("\n$ 检索到可能相关的文档：")
            prompt_parts.extend(rag_docs)
        if messages:
            prompt_parts.append("\n$ 下面是之前的消息缓冲区中的消息：")
            prompt_parts.extend(messages)
        if reply_text:
            prompt_parts.append("\n$ 用户引用了之前的消息：")
            prompt_parts.append(reply_text)

        if cfg.image_caption_model and attachments:
            image_caption_tasks = [get_image_caption(att) for att in attachments]
            # 并发获取图片转述，避免排队获取太慢
            image_captions = await asyncio.gather(*image_caption_tasks)
            caption_descriptions: list[str] = []
            for att, caption in zip(attachments, image_captions):
                if caption:
                    caption_descriptions.append(f"图片{att.get('displayName')}：{caption}")
            prompt_parts.append("\n$ 下面是这次的用户消息中的图片附件的文字转述：")
            prompt_parts.extend(caption_descriptions)

        if user_info := build_sender_info(session_info.nickname, session_info.user_id):
            prompt = f"{user_info}：{prompt}"
        if prompt:
            prompt_parts.append("\n$ 下面是这次的用户消息：")
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

    def get_session_messages_size(self, session_id: str) -> int:
        """获取会话消息缓冲区大小"""
        return len(self._sessions_messages.get(session_id, []))

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        async with await self._ensure_session_lock(session_id), self._global_lock:
            if session_id not in self._sessions_messages:
                self._sessions_messages[session_id] = deque(maxlen=cfg.session_messages_max_size)
            # deque(maxlen)会在溢出时自动丢弃最早的消息
            self._sessions_messages[session_id].append(prompt)

    async def reset_session(self, session_id: str):
        """删除会话，清空缓冲区。**此操作不可逆**"""
        # 先获取会话锁，确保同一时间只有一个协程在操作同一个会话。
        # 全局锁只保护字典访问，不包住耗时的客户端 RPC。
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                session = self._sessions.pop(session_id, None)
                self._memory_contexts.pop(session_id, None)
                if session_id in self._sessions_messages:
                    del self._sessions_messages[session_id]

            # 断开并删除现有会话
            try:
                if session:
                    await session.disconnect()
                await COPILOT_CLIENT.delete_session(session_id)
            except RuntimeError as e:
                logger.warning(f"删除会话{session_id}时发生错误: {e}")

    def _update_memory_context(self, session_info: SessionInfo) -> MemoryContext:
        context = self._memory_contexts.get(session_info.session_id)
        if context is None:
            context = MemoryContext.from_session_info(session_info)
            self._memory_contexts[session_info.session_id] = context
        else:
            context.update(session_info)
        return context


copilot = CopilotSessionManager()
