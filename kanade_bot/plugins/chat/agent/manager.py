"""聊天会话管理器（openai-agents SDK）。

职责与原 CopilotSessionManager 一致：管理会话历史（`LocalCompactionSession`
包装的 `SQLiteSession`）、会话锁、消息缓冲区、系统通知、群聊记忆身份切换，
并提供流式发送（watchdog 超时取消 + 空响应回退重发）、重置、中断、存储清理
等操作。
"""

import asyncio
import json
import platform
from collections import deque
from typing import Any

from agents import Agent, ItemHelpers, RunContextWrapper, Runner
from agents.items import MessageOutputItem, TResponseInputItem
from agents.mcp import MCPServer, MCPServerManager, MCPServerStreamableHttp, ToolFilter
from agents.mcp.server import MCPServerStreamableHttpParams
from agents.memory import SQLiteSession
from agents.sandbox import SandboxAgent
from agents.sandbox.capabilities import Shell
from agents.sandbox.session import SandboxSession
from nonebot import get_driver, logger
from openai.types.responses import ResponseInputMessageContentListParam
from openai_agents_context_compaction import LocalCompactionSession

from kanade_bot.utils.agents_runtime import (
    CONTINUE_PROMPT,
    begin_length_tracking,
    build_model_settings,
    get_length_tracked_model,
)
from kanade_bot.utils.parse import ImageInput, build_sender_info
from kanade_bot.utils.session import SessionInfo

from ..config import cfg
from .context import ChatContext
from .image_caption import get_image_caption
from .memory import MemoryContext, MemoryStore
from .sandbox import SandboxManager
from .tool import build_tools

agent = cfg.agent

FALLBACK_SYSTEM_PROMPT = "你是一只可爱的猫娘。"

EMPTY_RESPONSE_MAX_RETRIES = 2
"""空响应（整轮无文本输出且无工具调用，模型把答案写进了推理通道）的最大重发
次数，含首发共1+2次尝试。重发前会回退本轮写入的items，避免历史残留无效轮次。"""


class EmptyResponseError(RuntimeError):
    """重发耗尽后模型仍返回空响应（整轮无文本输出且无工具调用）。

    抛出前已回退本轮写入的items，历史中没有残留。"""


def _build_system_prompt() -> str:
    sp_path = agent.system_prompt_file_path
    if not sp_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {sp_path.absolute()}")
        return FALLBACK_SYSTEM_PROMPT

    sp = sp_path.read_text(encoding="utf-8")
    extras = agent.system_prompt_extras_paths

    for k, p in extras.items():
        if not p.is_file():
            logger.warning(f"系统提示词额外内容文件不存在，路径: {p.absolute()}")
            continue
        content = p.read_text(encoding="utf-8")
        sp = sp.replace(f"{{{{{k}}}}}", content)

    return sp


class ChatSessionManager:
    """聊天会话管理器：会话对象、消息缓冲区、会话锁与Agent运行"""

    system_prompt = _build_system_prompt()
    """系统提示词"""
    logger.trace(f"系统提示词:\n{system_prompt}")

    def __init__(self):
        self._memory_store = MemoryStore(
            cfg.memory_database_file_path,
            max_memories_per_scope=cfg.memory_max_records_per_scope,
        )
        self.memory_store = self._memory_store
        """记忆存储（供工具层访问）"""
        self._memory_contexts: dict[str, MemoryContext] = {}

        self._sessions: dict[str, LocalCompactionSession] = {}
        """会话对象缓存，键为会话ID，值为LocalCompactionSession（底层SQLiteSession）"""
        self._session_locks: dict[str, asyncio.Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID，值为Lock对象"""

        self._sessions_messages: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息，键为会话ID，值为消息列表"""
        self._sessions_system_notification: dict[str, str] = {}
        """会话系统通知，键为会话ID，值为系统通知内容"""

        self._streams: dict[str, Any] = {}
        """进行中的流式运行（RunResultStreaming），键为会话ID，用于中断"""

        self._global_lock = asyncio.Lock()
        """全局资源锁，对sessions字典的修改操作加锁，确保线程安全"""

        self._mcp_manager: MCPServerManager | None = None
        self._sandbox_manager: SandboxManager | None = None

        # Agent与工具静态化：所有动态性经 ChatContext 传递；
        # 模型用截断跟踪变体，支持输出截断时的拼接续写。
        # 沙箱能力只留 Shell：Filesystem 的 apply_patch 是 FREEFORM/grammar
        # 工具，仅Responses API支持，Chat Completions下会在工具转换时抛
        # UserError；文件编辑由模型用shell（echo/cat/sed等）完成
        common_kwargs = {
            "name": "kanade-bot-chat",
            "instructions": self._dynamic_instructions,
            "model": get_length_tracked_model(agent),
            "model_settings": build_model_settings(agent),
            "tools": build_tools(),
        }
        if cfg.sandbox.enabled:
            self._sandbox_manager = SandboxManager()
            self._agent: Agent[ChatContext] = SandboxAgent(
                capabilities=[Shell()],
                **common_kwargs,
            )
        else:
            self._agent: Agent[ChatContext] = Agent(**common_kwargs)

        driver = get_driver()
        driver.on_startup(self._start_mcp)
        driver.on_startup(self._load_sessions_messages_cache)
        driver.on_shutdown(self._save_sessions_messages_cache)
        driver.on_shutdown(self._shutdown)

    # ===== 消息缓冲区缓存 =====

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
                json.dump(data, f, ensure_ascii=False)
            logger.info(f"已保存{len(self._sessions_messages)}个会话的消息缓冲区缓存")
        except Exception as e:  # noqa: BLE001
            logger.exception(f"保存会话消息缓冲区缓存时发生错误: {e}")

    # ===== 生命周期 =====

    async def _start_mcp(self) -> None:
        """连接MCP服务器并把可用工具挂到Agent"""
        mcp_configs = agent.mcp_servers
        if not mcp_configs:
            return

        servers: list[MCPServer] = []
        for name, server_cfg in mcp_configs.items():
            params: MCPServerStreamableHttpParams = {"url": server_cfg.url}
            if server_cfg.headers:
                params["headers"] = server_cfg.headers

            tools = server_cfg.tools
            tool_filter: ToolFilter | None = None
            if tools and tools != ["*"]:
                tool_filter = {"allowed_tool_names": tools}

            servers.append(
                MCPServerStreamableHttp(
                    params=params,
                    name=name,
                    tool_filter=tool_filter,
                )
            )
        manager = MCPServerManager(servers, drop_failed_servers=True)
        await manager.__aenter__()
        self._mcp_manager = manager
        self._agent.mcp_servers = list(manager.active_servers)
        if manager.failed_servers:
            logger.warning(f"MCP服务器连接失败: {[s.name for s in manager.failed_servers]}")

    async def _shutdown(self):
        if self._mcp_manager is not None:
            try:
                await self._mcp_manager.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"关闭MCP服务器时发生错误: {e}")
            self._mcp_manager = None
        if self._sandbox_manager is not None:
            await self._sandbox_manager.destroy_all()

    # ===== Agent 动态指令 =====

    def _dynamic_instructions(self, ctx_wrapper: RunContextWrapper, _agent) -> str:
        """组装系统提示词：静态人格 + 会话动态段"""
        prompt = self.system_prompt
        info: SessionInfo = ctx_wrapper.context.session_info

        prompt += f"\n* Operationg System: {platform.system()}\n"

        if ctx_wrapper.context.sandbox is not None:
            workspace_root = ctx_wrapper.context.sandbox_root
            prompt += (
                f"\n* 你有一个Linux沙箱工作区（mirage虚拟文件系统，"
                f"由Landlock沙箱约束），可用exec_command执行shell命令。\n"
                f"  工作区根目录（绝对路径）：{workspace_root}\n"
                "  当前工作目录即为工作区根，文件读写与编辑用 cat/echo/sed，"
                "下载用curl，建目录用mkdir 等；\n"
                "  curl 请用 -s/-L/-o <文件>，**不要用 -m 或 -k**（内置curl暂不支持）；\n"
                "  工作区文件在会话间持久保留，发送给用户的文件/图片请用\n"
                "  send_file/send_image 工具从工作区发送。\n"
            )

        if group_info := build_sender_info(info.group_name, info.group_id):
            prompt += f"\n当前会话在群聊{group_info}中。\n"

        return prompt

    # ===== 会话与缓冲区 =====

    def _get_session(self, session_id: str) -> LocalCompactionSession:
        """获取（或惰性创建）会话对象"""
        session = self._sessions.get(session_id)
        if session is None:
            db_path = cfg.session.session_db_file_path
            db_path.parent.mkdir(parents=True, exist_ok=True)
            session = LocalCompactionSession(
                SQLiteSession(session_id, db_path),
                window_size=cfg.session.compaction_window_size,
            )
            self._sessions[session_id] = session
        return session

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

    async def add_system_notification(self, session_id: str, notification: str):
        """添加会话系统通知，将在下一次发送消息时附加到提示词中"""
        async with await self._ensure_session_lock(session_id), self._global_lock:
            self._sessions_system_notification[session_id] = notification

    async def add_message(self, session_id: str, prompt: str):
        """向会话缓冲区添加消息"""
        async with await self._ensure_session_lock(session_id), self._global_lock:
            if session_id not in self._sessions_messages:
                self._sessions_messages[session_id] = deque(maxlen=cfg.session_messages_max_size)
            # deque(maxlen)会在溢出时自动丢弃最早的消息
            self._sessions_messages[session_id].append(prompt)

    @staticmethod
    def _build_send_prompt(
        session_info: SessionInfo,
        prompt: str,
        *,
        rag_docs: list[str] | None = None,
        messages: deque[str] | None = None,
        reply_text: str | None = None,
        system_notification: str | None = None,
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

        if user_info := build_sender_info(session_info.nickname, session_info.user_id):
            prompt = f"{user_info}：{prompt}"
        if prompt:
            prompt_parts.append("\n$ 下面是这次用户对你的消息：")
            prompt_parts.append(prompt)

        if system_notification:
            prompt_parts.append("<system_notification>")
            prompt_parts.append(system_notification)
            prompt_parts.append("</system_notification>")

        return "\n".join(prompt_parts).strip()

    def _update_memory_context(self, session_info: SessionInfo) -> MemoryContext:
        context = self._memory_contexts.get(session_info.session_id)
        if context is None:
            context = MemoryContext.from_session_info(session_info)
            self._memory_contexts[session_info.session_id] = context
        else:
            context.update(session_info)
        return context

    # ===== 发送与流式 =====

    async def send_and_wait(
        self,
        session_info: SessionInfo,
        prompt: str,
        *,
        bot_id: str | None = None,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        images: list[ImageInput] | None = None,
        timeout: float = 60,
    ):
        """发送消息到会话，每条助手消息一到达就实时yield其内容。

        本方法是异步生成器，调用方通过 `async for` 逐条消费，以便及时处理。
        调用方必须完整消费本生成器，或使用`contextlib.aclosing` 包裹，
        以确保消息缓冲区清空和会话锁释放。

        没有任何可发送内容（无prompt、缓冲区为空且无引用消息）时不产出任何消息。

        prompt: 用户消息文本内容，如果为空，则仅使用缓冲区中的消息和引用消息
        images: 图片附件列表
        timeout: 相邻流事件间的间隔超时，超时后取消运行并抛出TimeoutError
        """
        session_id = session_info.session_id
        async with await self._ensure_session_lock(session_id):
            # Group sessions are shared by members; switch the tool context to
            # the current sender while the per-session lock is held.
            memory_context = self._update_memory_context(session_info)

            async with self._global_lock:
                messages = self._sessions_messages.get(session_id)
                if not prompt and not messages and not reply_text and not images:
                    # 没有任何新的消息可发送，直接返回（空生成器）
                    logger.info("发送给模型的消息为空，未触发生成")
                    return

                # 将系统通知附加到提示词中
                notice = self._sessions_system_notification.pop(session_id, None)

            send_prompt = self._build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                messages=messages,
                reply_text=reply_text,
                system_notification=notice,
            )

            input_items = await self._build_input_items(send_prompt, images)

            # 沙箱启用时获取（惰性创建）会话沙箱
            sandbox_session = None
            sandbox_root = None
            if self._sandbox_manager is not None:
                sandbox_session = await self._sandbox_manager.acquire(session_id)
                sandbox_root = self._sandbox_manager.workspace_root(session_id)

            context = ChatContext(
                session_info=session_info,
                bot_id=bot_id,
                memory_context=memory_context,
                sandbox=sandbox_session,
                sandbox_root=sandbox_root,
            )

            session = self._get_session(session_id)

            try:
                for attempt in range(1 + EMPTY_RESPONSE_MAX_RETRIES):
                    # 记录底层全量items数，用于空响应回退
                    baseline = len(await session._session.get_items())
                    produced = False
                    tool_called = False

                    current_input: str | list = input_items
                    continue_rounds = 0
                    while True:
                        tracker = begin_length_tracking()
                        result = Runner.run_streamed(
                            self._agent,
                            current_input,
                            context=context,
                            session=session,
                            run_config=self._sandbox_run_config(sandbox_session),
                        )
                        self._streams[session_id] = result
                        try:
                            event_iter = result.stream_events().__aiter__()
                            while True:
                                try:
                                    event = await asyncio.wait_for(
                                        event_iter.__anext__(), timeout=timeout
                                    )
                                except StopAsyncIteration:
                                    break
                                except TimeoutError:
                                    # 相邻事件间隔超时：取消运行（中断在途LLM请求与
                                    # 工具循环，进程内取消即真正停止）
                                    result.cancel()
                                    raise TimeoutError(
                                        f"Timeout after {timeout}s waiting for stream events"
                                    ) from None

                                if event.type != "run_item_stream_event":
                                    continue
                                if event.name == "tool_called":
                                    tool_called = True
                                elif event.name == "message_output_created" and isinstance(
                                    event.item, MessageOutputItem
                                ):
                                    text = ItemHelpers.text_message_output(event.item)
                                    if text.strip():
                                        produced = True
                                        yield text
                        finally:
                            self._streams.pop(session_id, None)

                        # 输出因max_output_tokens截断：续写（session已携带
                        # 含半截助手消息的历史，续写输入只需一句提示）
                        if tracker.get("finish_reason") == "length":
                            continue_rounds += 1
                            logger.warning(
                                f"会话{session_id}输出因max_output_tokens截断"
                                f"（续写第{continue_rounds}次）"
                            )
                            current_input = CONTINUE_PROMPT
                            continue
                        break

                    if produced or tool_called:
                        return

                    # 整轮空响应：回退本轮写入的items后重发
                    await self._rollback_items(session, baseline)
                    if attempt < EMPTY_RESPONSE_MAX_RETRIES:
                        logger.warning(
                            f"会话{session_id}本轮回复为空"
                            f"（尝试{attempt + 1}/{1 + EMPTY_RESPONSE_MAX_RETRIES}），"
                            f"已回退本轮items并重发"
                        )
                        continue
                    raise EmptyResponseError(
                        f"会话{session_id}连续{attempt + 1}次回复为空"
                        f"（无文本输出且无工具调用），已停止重试"
                    )
            finally:
                async with self._global_lock:
                    # 清空消息缓冲区
                    if session_id in self._sessions_messages:
                        self._sessions_messages[session_id].clear()

    async def _build_input_items(
        self, send_prompt: str, images: list[ImageInput] | None
    ) -> list[TResponseInputItem]:
        """构建输入items：文本 + 图片（vision直传，否则转述/占位）"""
        content: ResponseInputMessageContentListParam = []

        if agent.vision:
            content.append({"type": "input_text", "text": send_prompt})
            for image in images or []:
                content.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{image.mime_type};base64,{image.data}",
                        "detail": "auto",
                    }
                )
            return [{"role": "user", "content": content}]

        # 模型无视觉：转述
        parts = [send_prompt]

        for image in images or []:
            # data为None表示图片内容获取失败，仅剩名称占位，无法转述
            if cfg.image_caption and image.data and image.mime_type:
                caption = await get_image_caption(image.data, image.mime_type)
                if caption:
                    parts.append(f"\n$ 图片 {image.name} 的文字描述: \n{caption}")
                    continue
            parts.append(f"\n[收到图片 {image.name}，但当前无法查看图片内容]")
        content.append({"type": "input_text", "text": "\n".join(parts)})
        return [{"role": "user", "content": content}]

    @staticmethod
    async def _rollback_items(session: LocalCompactionSession, baseline: int) -> None:
        """回退会话底层items到baseline数量（从尾部弹出本轮写入的items）"""
        underlying = session._session
        current = len(await underlying.get_items())
        for _ in range(current - baseline):
            await underlying.pop_item()

    # ===== 管理操作 =====

    def _sandbox_run_config(self, sandbox_session: SandboxSession | None):
        """构造运行配置；沙箱会话存在时注入"""
        if sandbox_session is None:
            return None
        from agents import RunConfig
        from agents.run_config import SandboxRunConfig

        return RunConfig(sandbox=SandboxRunConfig(session=sandbox_session))

    async def reset_session(self, session_id: str):
        """清空会话历史、缓冲区、记忆上下文与沙箱。**此操作不可逆**"""
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                session = self._sessions.pop(session_id, None)
                self._memory_contexts.pop(session_id, None)
                if session_id in self._sessions_messages:
                    del self._sessions_messages[session_id]

            if session:
                try:
                    await session.clear_session()
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"清空会话{session_id}历史时发生错误: {e}")

            if self._sandbox_manager is not None:
                # 不保留工作区：重置即彻底清除沙箱文件
                await self._sandbox_manager.destroy(session_id)
                self._sandbox_manager.delete_workspace(session_id)

    async def interrupt_session_turn(self, session_id: str) -> bool | None:
        """手动中断会话当前正在运行的回复，不影响后续消息

        会话不存在（未在生成中）时返回None；中断成功返回True。
        """
        async with self._global_lock:
            stream = self._streams.get(session_id)
        if stream is None:
            return None
        stream.cancel()
        return True

    async def compact_session(self, session_id: str) -> dict[str, int] | None:
        """物理清理会话存储：删除滑动窗口之外的items，控制数据库体积

        返回统计信息（总items、保留items、删除items），会话不存在时返回None。
        """
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                session = self._sessions.get(session_id)
            if session is None:
                return None

            underlying = session._session
            all_items = await underlying.get_items()
            windowed = await session.get_items()
            if len(all_items) == len(windowed):
                return {"total": len(all_items), "kept": len(windowed), "removed": 0}

            await underlying.clear_session()
            await underlying.add_items(windowed)
            logger.info(f"已清理会话{session_id}窗口外items：{len(all_items)} → {len(windowed)}")
            return {
                "total": len(all_items),
                "kept": len(windowed),
                "removed": len(all_items) - len(windowed),
            }


chat_manager = ChatSessionManager()
