"""聊天会话管理器（Pydantic AI）。

职责：管理会话历史（`SessionStore`：append-only 全量存储 + 压缩事件标记）、
会话锁、消息缓冲区、系统通知、群聊记忆身份切换，并提供流式发送
（watchdog 超时取消 + 空响应回退重发 + 截断续写）、重置、中断、存储统计等操作。

流式使用 `agent.run_stream_events()`——**完整 agent 循环**（工具调用后继续
生成），而不是 `run_stream()`（后者只提交首个匹配输出，可能跳过工具调用）。
"""

import asyncio
import base64
import json
import platform
from collections import deque
from typing import Any

from nonebot import get_driver, logger
from pydantic_ai import Agent, FunctionToolCallEvent, PartEndEvent, RunContext
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelResponse,
    TextPart,
    UserContent,
)
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.filtered import FilteredToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness import FileSystem, Shell

from kanade_bot.utils.pai_runtime import CONTINUE_PROMPT, build_model_settings, get_model
from kanade_bot.utils.parse import ImageInput, build_sender_info
from kanade_bot.utils.session import SessionInfo

from ..config import cfg
from .compaction import CompactionParams, RecordingCompaction, build_compaction_capability
from .deps import ChatDeps
from .image_caption import get_image_caption
from .memory import MemoryContext, MemoryStore
from .sandbox import SandboxManager
from .session_store import SessionStore
from .tool import build_tools, prepare_tools

agent = cfg.agent

FALLBACK_SYSTEM_PROMPT = "你是一只可爱的猫娘。"

EMPTY_RESPONSE_MAX_RETRIES = 2
"""空响应（整轮无文本输出且无工具调用，模型把答案写进了推理通道）的最大重发
次数，含首发共1+2次尝试。重发前会回退本轮写入的消息，避免历史残留无效轮次。"""

REQUEST_LIMIT = 100
"""单次会话运行的模型请求数上限。

Pydantic AI 默认 50；本项目存在长工具链（沙箱 shell + 多次文件读写），
偏紧会误伤，故显式放宽。设为 `None` 可关闭该保护。
"""

MAX_CONTINUATIONS = 5
"""单轮内因 `max_output_tokens` 截断而续写的最大次数，防止无限续写"""


class EmptyResponseError(RuntimeError):
    """重发耗尽后模型仍返回空响应（整轮无文本输出且无工具调用）。

    抛出前已回退本轮写入的消息，历史中没有残留。"""


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
    """聊天会话管理器：会话存储、消息缓冲区、会话锁与Agent运行"""

    system_prompt = _build_system_prompt()
    """系统提示词"""
    logger.trace(f"系统提示词:\n{system_prompt}")

    def __init__(self):
        self._memory_store = MemoryStore(
            cfg.memory.database_file_path,
            max_memories_per_scope=cfg.memory.max_records_per_scope,
        )
        self.memory_store = self._memory_store
        """记忆存储（供工具层访问）"""
        self._memory_contexts: dict[str, MemoryContext] = {}

        compaction_cfg = cfg.compaction
        self._compaction_params = CompactionParams(
            trigger_fraction=compaction_cfg.trigger_fraction,
            keep_pairs=compaction_cfg.keep_pairs,
            min_clear_tokens=compaction_cfg.min_clear_tokens,
            context_window=compaction_cfg.context_window,
            summary_target_fraction=compaction_cfg.summary_target_fraction,
            summary_model=compaction_cfg.summary_model,
            summary_keep_messages=compaction_cfg.summary_keep_messages,
        )
        self._compaction: RecordingCompaction = build_compaction_capability(self._compaction_params)
        self._store = SessionStore(cfg.session.db_file_path)
        """会话存储（append-only 全量 + 压缩事件标记）"""

        self._session_locks: dict[str, asyncio.Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID"""

        self._sessions_messages: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息"""
        self._sessions_system_notification: dict[str, str] = {}
        """会话系统通知，键为会话ID，值为系统通知内容"""

        self._streams: dict[str, Any] = {}
        """进行中的流式运行（`AgentRunEvents`），键为会话ID，用于中断"""

        self._global_lock = asyncio.Lock()
        """全局资源锁，对各字典的修改操作加锁，确保协程安全"""

        self._mcp_toolsets: list[AbstractToolset[ChatDeps]] = []
        """MCP 工具集。`Agent.toolsets` 是只读属性，而 MCP 连接发生在 Agent 构造之后的
        on_startup 阶段，因此每次运行通过 per-run toolsets 参数传入。"""
        self._sandbox_manager: SandboxManager | None = None

        # capability 注册顺序（硬性约束）：**压缩 → 工具过滤**。
        # 官方明确：request-only 注入器必须排在压缩之后，否则会被压缩丢弃；
        # 反过来压缩若排在工具过滤之后，会把已过滤的结果再次改写。
        capabilities: list[Any] = [self._compaction, PrepareTools(prepare_tools)]
        if cfg.sandbox.enabled:
            self._sandbox_manager = SandboxManager()
            # 官方 Shell/FileSystem 能力：命令与文件读写都走 `ctx.workspace`，
            # 即本项目的 mirage `MirageBackend`。命令名黑名单**清空**：
            # 隔离边界由 mirage VFS + Landlock 提供，不需要工具层再拦一道
            # （默认黑名单含 rm/dd 等，会影响模型的正常编辑与清理操作）。
            capabilities.append(Shell(denied_commands=[], default_timeout=120.0))
            capabilities.append(FileSystem())

        self._agent: Agent[ChatDeps] = Agent(
            name="kanade-bot-chat",
            instructions=self._dynamic_instructions,
            deps_type=ChatDeps,
            model=get_model(agent),
            model_settings=build_model_settings(agent),
            tools=build_tools(),
            capabilities=capabilities,
        )

        driver = get_driver()
        driver.on_startup(self._start_mcp)
        driver.on_startup(self._load_sessions_messages_cache)
        driver.on_shutdown(self._save_sessions_messages_cache)
        driver.on_shutdown(self._shutdown)

    # ===== 消息缓冲区缓存 =====

    def _load_sessions_messages_cache(self):
        """加载会话消息缓冲区缓存"""
        cache_file = cfg.session.buffer_cache_file_path
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
                messages, maxlen=cfg.session.buffer_max_size
            )
        logger.info(f"已加载{len(self._sessions_messages)}个会话的消息缓冲区缓存")

    def _save_sessions_messages_cache(self):
        """保存会话消息缓冲区缓存"""
        cache_file = cfg.session.buffer_cache_file_path
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
        """连接MCP服务器并把可用工具集挂到Agent"""
        mcp_configs = agent.mcp_servers
        if not mcp_configs:
            return

        toolsets: list[AbstractToolset[ChatDeps]] = []
        failed: list[str] = []
        for name, server_cfg in mcp_configs.items():
            headers = dict(server_cfg.headers) if server_cfg.headers else None
            try:
                toolset: AbstractToolset[ChatDeps] = MCPToolset(server_cfg.url, headers=headers)
                await toolset.__aenter__()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"MCP服务器{name}连接失败: {e}")
                failed.append(name)
                continue

            allowed = server_cfg.tools
            if allowed and allowed != ["*"]:
                # 工具白名单：关闭的工具直接不进 schema
                allow_set = frozenset(allowed)
                toolset = FilteredToolset(
                    toolset, lambda ctx, td, allow=allow_set: td.name in allow
                )

            toolsets.append(toolset)

        if failed:
            logger.warning(f"MCP服务器连接失败: {failed}")
        self._mcp_toolsets = toolsets
        logger.info(f"已加载{len(toolsets)}个MCP工具集")

    async def _shutdown(self):
        for toolset in self._mcp_toolsets:
            try:
                await toolset.__aexit__(None, None, None)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"关闭MCP服务器时发生错误: {e}")
        self._mcp_toolsets = []
        if self._sandbox_manager is not None:
            await self._sandbox_manager.destroy_all()

    # ===== Agent 动态指令 =====

    def _dynamic_instructions(self, ctx: RunContext[ChatDeps]) -> str:
        """组装系统提示词：静态人格 + 会话动态段"""
        prompt = self.system_prompt
        info: SessionInfo = ctx.deps.session_info

        prompt += f"\n* Operationg System: {platform.system()}\n"

        if ctx.deps.sandbox is not None:
            workspace_root = ctx.deps.sandbox_root
            prompt += (
                f"\n* 你有一个Linux沙箱工作区（mirage虚拟文件系统，"
                f"由Landlock沙箱约束），可用run_command执行shell命令。\n"
                f"  工作区根目录（绝对路径）：{workspace_root}\n"
                "  当前工作目录即为工作区根，文件读写与编辑用 read_file/write_file/edit_file，"
                "搜索用 search_files/find_files，建目录用create_directory，"
                "下载用curl，建目录用mkdir 等；\n"
                "  curl 请用 -s/-L/-o <文件>，**不要用 -m 或 -k**（内置curl暂不支持）；\n"
                "  工作区文件在会话间持久保留，发送给用户的文件/图片请用\n"
                "  send_file/send_image 工具从工作区发送。\n"
            )

        if group_info := build_sender_info(info.group_name, info.group_id):
            prompt += f"\n当前会话在群聊{group_info}中。\n"

        return prompt

    # ===== 会话与缓冲区 =====

    async def _ensure_session_lock(self, session_id: str) -> asyncio.Lock:
        """确保会话锁存在并返回"""
        # 不要在持有全局锁的情况下调用此函数，以避免死锁
        if session_id not in self._session_locks:
            async with self._global_lock:
                self._session_locks.setdefault(session_id, asyncio.Lock())
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
                self._sessions_messages[session_id] = deque(maxlen=cfg.session.buffer_max_size)
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

            user_content = await self._build_user_content(send_prompt, images)

            # 沙箱启用时获取（惰性创建）会话沙箱
            sandbox_session = None
            sandbox_root = None
            if self._sandbox_manager is not None:
                sandbox_session = await self._sandbox_manager.acquire(session_id)
                sandbox_root = self._sandbox_manager.workspace_root(session_id)

            deps = ChatDeps(
                session_info=session_info,
                bot_id=bot_id,
                memory_context=memory_context,
                sandbox=sandbox_session,
                sandbox_root=sandbox_root,
            )

            # 恢复历史：全量原始消息 + 按 marks 重放压缩
            history = await self._store.restore(session_id, params=self._compaction_params)

            try:
                for attempt in range(1 + EMPTY_RESPONSE_MAX_RETRIES):
                    # 记录本轮写入前的消息数，用于空响应回退
                    baseline = len(history)
                    produced = False
                    tool_called = False
                    continuations = 0

                    current_input: str | list[UserContent] = user_content
                    while True:
                        run: list[ModelMessage] | None = None
                        events = self._agent.run_stream_events(
                            current_input,
                            message_history=history,
                            deps=deps,
                            usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
                            toolsets=self._mcp_toolsets or None,
                            workspace=(
                                sandbox_session.backend if sandbox_session is not None else None
                            ),
                        )
                        async with events as stream:
                            self._streams[session_id] = stream
                            try:
                                iterator = stream.__aiter__()
                                while True:
                                    try:
                                        event = await asyncio.wait_for(
                                            iterator.__anext__(), timeout=timeout
                                        )
                                    except StopAsyncIteration:
                                        break
                                    except TimeoutError:
                                        # 相邻事件间隔超时：取消运行（中断在途 LLM 请求
                                        # 与工具循环，进程内取消即真正停止）
                                        stream.cancel()
                                        raise TimeoutError(
                                            f"Timeout after {timeout}s waiting for stream events"
                                        ) from None

                                    if isinstance(event, PartEndEvent):
                                        part = event.part
                                        if isinstance(part, TextPart) and part.content.strip():
                                            produced = True
                                            yield part.content
                                    elif isinstance(event, FunctionToolCallEvent):
                                        tool_called = True
                            finally:
                                self._streams.pop(session_id, None)

                            run = stream.new_messages()

                        if run:
                            history = [*history, *run]
                            await self._store.append(session_id, run)

                        # 输出因 max_output_tokens 截断：续写（历史已入库，
                        # 续写输入只需一句提示）
                        # 注意 `run` 末条一定是 ModelResponse，finish_reason 只存在于
                        # ModelResponse 上（ModelRequest 无此字段）
                        if (
                            run
                            and isinstance(run[-1], ModelResponse)
                            and run[-1].finish_reason == "length"
                        ):
                            continuations += 1
                            if continuations > MAX_CONTINUATIONS:
                                logger.warning(
                                    f"会话{session_id}连续{continuations}次续写仍被截断，停止续写"
                                )
                                break
                            logger.warning(
                                f"会话{session_id}输出因max_output_tokens截断"
                                f"（续写第{continuations}次）"
                            )
                            current_input = CONTINUE_PROMPT
                            continue
                        break

                    # 记录本轮发生过的压缩（一条 mark，恢复时重放）
                    await self._record_compaction(session_id, history)

                    if produced or tool_called:
                        return

                    # 整轮空响应：回退本轮写入的消息后重发
                    history = history[:baseline]
                    await self._store.truncate(session_id, baseline)
                    if attempt < EMPTY_RESPONSE_MAX_RETRIES:
                        logger.warning(
                            f"会话{session_id}本轮回复为空"
                            f"（尝试{attempt + 1}/{1 + EMPTY_RESPONSE_MAX_RETRIES}），"
                            f"已回退本轮消息并重发"
                        )
                        current_input = user_content
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

    async def _record_compaction(self, session_id: str, history: list[ModelMessage]) -> None:
        """本轮结束后把发生的压缩记成一条 mark（恢复时重放）"""
        mark = await self._compaction.take_mark(history)
        if mark is not None:
            await self._store.add_compaction_mark(session_id, mark)
            logger.debug(
                f"会话{session_id}记录压缩事件：策略={mark.strategy}，可重放={mark.result is None}"
            )

    async def _build_user_content(
        self, send_prompt: str, images: list[ImageInput] | None
    ) -> list[UserContent]:
        """构建本轮用户输入：文本 + 图片（vision 直传，否则转述/占位）"""
        content: list[UserContent] = []

        if agent.vision:
            content.append(send_prompt)
            for image in images or []:
                content.append(
                    BinaryContent(
                        data=base64.b64decode(image.data or ""),
                        media_type=image.mime_type or "image/jpeg",
                    )
                )
            return content

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
        content.append("\n".join(parts))
        return content

    # ===== 管理操作 =====

    async def reset_session(self, session_id: str):
        """清空会话历史、缓冲区、记忆上下文与沙箱。**此操作不可逆**"""
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                self._memory_contexts.pop(session_id, None)
                self._sessions_messages.pop(session_id, None)

            try:
                await self._store.clear(session_id)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"清空会话{session_id}历史时发生错误: {e}")

            if self._sandbox_manager is not None:
                # 不保留工作区：重置即彻底清除沙箱文件
                await self._sandbox_manager.destroy(session_id)
                self._sandbox_manager.delete_workspace(session_id)

    async def interrupt_session_turn(self, session_id: str) -> bool | None:
        """手动中断会话当前正在运行的回复，不影响后续消息

        会话不存在（未在生成中）时返回 None；中断成功返回 True。
        """
        async with self._global_lock:
            stream = self._streams.get(session_id)
        if stream is None:
            return None
        stream.cancel()
        return True

    async def compact_session(self, session_id: str) -> dict[str, int] | None:
        """统计会话存储：DB 全量保留，返回（全量条数，实际发送条数）

        DB 为 append-only 全量保留，本命令只读不写：报告当前压缩后
        实际发送给模型的消息数。会话无历史时返回 None。
        """
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            total = await self._store.count(session_id)
            if total == 0:
                return None
            kept = len(await self._store.restore(session_id, params=self._compaction_params))
            logger.info(f"会话{session_id}存储全量保留：共{total}条，压缩后实际发送{kept}条")
            return {"total": total, "kept": kept, "removed": 0}


chat_manager = ChatSessionManager()
