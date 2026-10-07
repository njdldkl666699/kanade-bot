import asyncio
import base64
import json
import mimetypes
import os
import uuid
from collections import deque
from io import BytesIO
from pathlib import Path
from typing import Any

from nonebot import get_driver, logger
from pydantic_ai import Agent, AgentRunEvents, FunctionToolCallEvent, PartEndEvent, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import BinaryContent, ModelMessage, ModelResponse, TextPart, UserContent
from pydantic_ai.models import Model
from pydantic_ai.toolsets.abstract import AbstractToolset
from pydantic_ai.toolsets.filtered import FilteredToolset
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_backends import ConsoleCapability
from pydantic_ai_backends.permissions import PERMISSIVE_RULESET
from pydantic_ai_harness.compaction import (
    compact_now,
    estimate_context_tokens,
    resolve_context_window,
)

from kanade_bot.utils.billing import UsageCallback
from kanade_bot.utils.pai_runtime import CONTINUE_PROMPT, build_model_settings, get_model
from kanade_bot.utils.parse import ImageInput, build_sender_info
from kanade_bot.utils.session import SessionInfo

from ..config import CompactionConfig, cfg
from .compaction import (
    RecordingCompaction,
    build_compaction_capability,
    build_summary,
    build_summary_mark,
    extract_summary,
)
from .deps import ChatDeps
from .image_caption import get_image_caption
from .memory import MemoryContext, MemoryStore
from .prompt import ChatPrompt, current_time_line
from .sandbox import SandboxManager, SandboxSession, SandboxWorkspaceCapability
from .session_store import SessionStore
from .tool import build_tools

EMPTY_RESPONSE_MAX_RETRIES = 2
"""空响应（一轮请求无文本输出且无工具调用）的最大重发次数"""

REQUEST_LIMIT: int | None = None
"""单次会话运行的模型请求数上限。

Pydantic AI 默认 50；设为 `None` 可关闭该保护。
"""

MAX_CONTINUATIONS = 5
"""单轮内因length截断而续写的最大次数"""

COMPACT_LOCK_TIMEOUT = 3
"""手动压缩等待会话锁的超时秒数，超时视为会话正在处理中"""

MAX_WORKSPACE_ENTRIES = 10
"""会话统计最多展示的工作区条目数"""

WORKSPACE_WALK_DEPTH = 3
"""会话统计遍历工作区的最大目录深度"""


def _format_size(size: int) -> str:
    """字节数转人类可读文本"""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _list_workspace_files(root: Path) -> list[str]:
    """列出工作区条目（限深限量），返回 `相对路径 (大小)` 文本列表"""
    entries: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root).parts)
        if depth >= WORKSPACE_WALK_DEPTH:
            dirnames[:] = []  # 超过深度不再下钻
        for name in sorted(filenames):
            path = Path(dirpath) / name
            entries.append(f"{path.relative_to(root)} ({_format_size(path.stat().st_size)})")
            if len(entries) >= MAX_WORKSPACE_ENTRIES:
                entries.append("…（超出部分省略）")
                return entries
        for name in sorted(dirnames):
            entries.append(f"{Path(dirpath, name).relative_to(root)}/")
            if len(entries) >= MAX_WORKSPACE_ENTRIES:
                entries.append("…（超出部分省略）")
                return entries
    return entries


class EmptyResponseError(RuntimeError):
    """重发耗尽后模型仍返回空响应

    抛出前已回退本轮写入的消息"""


class ChatSessionManager:
    """聊天会话管理器：会话存储、消息缓冲区、会话锁与Agent运行"""

    def __init__(self):
        self._store = SessionStore(cfg.session.db_file_path)
        """会话存储"""

        self.memory_store = MemoryStore(
            cfg.memory.database_file_path,
            max_memories_per_scope=cfg.memory.max_records_per_scope,
        )
        """记忆存储"""
        self._memory_contexts: dict[str, MemoryContext] = {}

        self._compaction_params: CompactionConfig = cfg.compaction
        self._compaction: RecordingCompaction = build_compaction_capability(self._compaction_params)

        self._prompt = ChatPrompt(cfg)
        """模块化系统提示词渲染器"""

        self._sessions_messages: dict[str, deque[str]] = {}
        """会话消息缓冲区，用于存储尚未发送到模型的消息"""
        self._sessions_system_notification: dict[str, str] = {}
        """会话系统通知，键为会话ID，值为系统通知内容"""

        self._streams: dict[str, AgentRunEvents[str]] = {}
        """进行中的流式运行，键为会话ID，用于中断"""

        self._session_locks: dict[str, asyncio.Lock] = {}
        """会话锁，确保同一时间只有一个协程在操作同一个会话，键为会话ID"""
        self._global_lock = asyncio.Lock()
        """全局资源锁，对各字典的修改操作加锁，确保协程安全"""

        self._mcp_toolsets: list[AbstractToolset[ChatDeps]] = []
        """MCP 工具集"""
        self._sandbox_manager: SandboxManager | None = None

        capabilities: list[AbstractCapability] = [self._compaction]
        if cfg.sandbox.enabled:
            self._sandbox_manager = SandboxManager()
            capabilities.extend(
                [
                    SandboxWorkspaceCapability(),
                    ConsoleCapability(
                        permissions=PERMISSIVE_RULESET,
                        image_support=cfg.agent.vision,
                        profile="agent",
                    ),
                ]
            )

        self._agent: Agent[ChatDeps] = Agent(
            name="kanade-bot-chat",
            instructions=[
                *self._prompt.static_instructions,
                self._session_instructions,
            ],
            deps_type=ChatDeps,
            model=get_model(cfg.agent),
            model_settings=build_model_settings(cfg.agent),
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
        except Exception as e:
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
        except Exception as e:
            logger.exception(f"保存会话消息缓冲区缓存时发生错误: {e}")

    # ===== 生命周期 =====

    async def _start_mcp(self) -> None:
        """连接MCP服务器并把可用工具集挂到Agent"""
        mcp_configs = cfg.agent.mcp_servers
        if not mcp_configs:
            return

        toolsets: list[AbstractToolset[ChatDeps]] = []
        failed: list[str] = []
        for name, server_cfg in mcp_configs.items():
            headers = dict(server_cfg.headers) if server_cfg.headers else None
            try:
                toolset: AbstractToolset[ChatDeps] = MCPToolset(server_cfg.url, headers=headers)
                await toolset.__aenter__()
            except Exception as e:
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
        self._prompt.set_mcp_available(bool(toolsets))
        logger.info(f"已加载{len(toolsets)}个MCP工具集")

    async def _shutdown(self):
        if self._sandbox_manager is not None:
            await self._sandbox_manager.close_all()
        for toolset in self._mcp_toolsets:
            try:
                await toolset.__aexit__(None, None, None)
            except Exception as e:
                logger.warning(f"关闭MCP服务器时发生错误: {e}")
        self._mcp_toolsets = []

    # ===== Agent 会话层指令 =====

    def _session_instructions(self, ctx: RunContext[ChatDeps]) -> str:
        """按当前会话渲染会话层提示词"""
        return self._prompt.session_instructions(ctx.deps)

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

        prompt_parts.append(current_time_line())

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
        on_usage: UsageCallback | None = None,
        system_notification: str | None = None,
    ):
        """发送消息到会话，每条助手消息一到达就实时yield其内容。

        本方法是异步生成器，调用方通过 `async for` 逐条消费，以便及时处理。
        调用方必须完整消费本生成器，或使用`contextlib.aclosing` 包裹，
        以确保消息缓冲区清空和会话锁释放。

        没有任何可发送内容（无prompt、缓冲区为空且无引用消息）时不产出任何消息。

        prompt: 用户消息文本内容，如果为空，则仅使用缓冲区中的消息和引用消息
        images: 图片附件列表
        timeout: 相邻流事件间的间隔超时，超时后取消运行并抛出TimeoutError
        on_usage: 轮次正常结束时回调；异常或中途取消时不回调
        system_notification: 显式注入本轮的系统通知；不消费排队通知槽位，且单独视为可运行内容
        """
        session_id = session_info.session_id
        async with await self._ensure_session_lock(session_id):
            # Group sessions are shared by members; switch the tool context to
            # the current sender while the per-session lock is held.
            memory_context = self._update_memory_context(session_info)

            async with self._global_lock:
                messages = self._sessions_messages.get(session_id)
                if system_notification is not None:
                    # 显式注入：不消费排队槽位，排队通知留待下一轮
                    notice = system_notification
                else:
                    # 将系统通知附加到提示词中
                    notice = self._sessions_system_notification.pop(session_id, None)
                if not prompt and not messages and not reply_text and not images and not notice:
                    # 没有任何新的消息可发送，直接返回（空生成器）
                    logger.info("发送给模型的消息为空，未触发生成")
                    return

            send_prompt = self._build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                messages=messages,
                reply_text=reply_text,
                system_notification=notice,
            )

            # 沙箱启用时获取会话常驻沙箱（首次使用时创建，跨轮保留）
            sandbox_session = None
            sandbox_root = None
            staged_images: list[str] = []
            if self._sandbox_manager is not None:
                sandbox_session = await self._sandbox_manager.create(session_id)
                sandbox_root = self._sandbox_manager.workspace_root(session_id)
                # 用户图片写入沙箱工作区
                staged_images = await self._stage_images_to_sandbox(sandbox_session, images)
            if staged_images:
                send_prompt += (
                    "\n\n$ 本轮用户发送的图片已保存到沙箱工作区"
                    "（工具可用这些相对路径访问）：\n" + "\n".join(f"- {p}" for p in staged_images)
                )

            user_content = await self._build_user_content(send_prompt, images)

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
                total_usage = RunUsage()
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
                                        # 相邻事件间隔超时：取消运行
                                        # 中断在途 LLM 请求与工具循环，进程内取消即真正停止
                                        stream.cancel()
                                        raise TimeoutError(
                                            f"Timeout after {timeout}s waiting for stream events"
                                        )

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
                        # 累计本次运行的usage（含空响应重发与续写）
                        total_usage.incr(stream.usage)

                        # 输出因 max_output_tokens 截断：续写
                        # 历史已入库，续写输入只需一句提示
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

                    # 记录本轮发生过的压缩
                    await self._record_compaction(session_id, history)

                    if produced or tool_called:
                        if on_usage is not None:
                            on_usage(total_usage, produced)
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
                # 常驻沙箱不在此处关闭：跨轮保留 shell 会话状态，
                # 仅在会话重置或进程退出时关闭
                async with self._global_lock:
                    # 清空消息缓冲区
                    if session_id in self._sessions_messages:
                        self._sessions_messages[session_id].clear()

    async def _record_compaction(self, session_id: str, history: list[ModelMessage]) -> None:
        """本轮结束后把发生的压缩记成一条 mark"""
        mark = await self._compaction.take_mark(history)
        if mark is not None:
            await self._store.add_compaction_mark(session_id, mark)
            logger.debug(
                f"会话{session_id}记录压缩事件：策略={mark.strategy}，可重放={mark.result is None}"
            )

    @staticmethod
    async def _stage_images_to_sandbox(
        sandbox: SandboxSession, images: list[ImageInput] | None
    ) -> list[str]:
        """把本轮用户图片写入沙箱工作区 images/ 目录，返回相对路径列表"""
        staged: list[str] = []
        for image in images or []:
            if not image.data:
                continue
            data = base64.b64decode(image.data)
            name = Path(image.name).name or "image"
            if not Path(name).suffix and image.mime_type:
                name += mimetypes.guess_extension(image.mime_type) or ".jpg"
            target = Path("images") / name
            try:
                existing = (await sandbox.read(target)).read()
            except FileNotFoundError:
                existing = None
            if existing == data:
                staged.append(str(target))  # 同一张图重发，直接复用
                continue
            if existing is not None:
                target = Path("images") / f"{uuid.uuid4().hex[:8]}_{name}"
            try:
                await sandbox.write(target, BytesIO(data))
            except Exception as e:
                logger.warning(f"暂存图片到沙箱工作区失败: {image.name}: {e}")
                continue
            staged.append(str(target))
        return staged

    async def _build_user_content(
        self, send_prompt: str, images: list[ImageInput] | None
    ) -> list[UserContent]:
        """构建本轮用户输入：文本 + 图片"""
        content: list[UserContent] = []

        if cfg.agent.vision:
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
            parts.append(f"\n$ 收到图片 {image.name}，但当前无法查看图片内容")
        content.append("\n".join(parts))
        return content

    # ===== 管理操作 =====

    async def reset_session(self, session_id: str):
        """清空会话历史、缓冲区、记忆上下文并关闭沙箱。**此操作不可逆**"""
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            async with self._global_lock:
                self._memory_contexts.pop(session_id, None)
                self._sessions_messages.pop(session_id, None)

            try:
                await self._store.clear(session_id)
            except Exception as e:
                logger.warning(f"清空会话{session_id}历史时发生错误: {e}")

            if self._sandbox_manager is not None:
                await self._sandbox_manager.close(session_id)

    async def clear_workspace(self, session_id: str) -> bool | None:
        """关闭会话沙箱并删除工作区文件。**此操作不可逆**

        沙箱未启用时返回 None；工作区目录不存在（无内容可删）返回 False；
        删除成功返回 True。会话历史不受影响。
        """
        if self._sandbox_manager is None:
            return None
        workspace_root = Path(self._sandbox_manager.workspace_root(session_id))
        existed = workspace_root.is_dir()
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            await self._sandbox_manager.close(session_id)
            self._sandbox_manager.delete_workspace(session_id)
        return existed

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

    async def compact_session(self, session_id: str) -> dict[str, Any] | None:
        """手动执行一次 LLM 总结级压缩"""
        lock = await self._ensure_session_lock(session_id)
        try:
            async with asyncio.timeout(COMPACT_LOCK_TIMEOUT):
                await lock.acquire()
        except TimeoutError:
            raise RuntimeError(
                f"会话正在处理中（{COMPACT_LOCK_TIMEOUT}s内未获得会话锁），可先使用 /中断会话"
            ) from None

        try:
            total = await self._store.count(session_id)
            if total == 0:
                return None

            messages = await self._store.restore(session_id, params=self._compaction_params)
            before = len(messages)
            model = self._agent.model
            assert isinstance(model, Model), "Agent 构造时传入的必为 Model 实例"

            compacted = await compact_now(
                build_summary(self._compaction_params),
                messages,
                model=model,
                conversation_id=session_id,
            )
            if compacted is messages:
                # 历史全落在保留尾部内：无可安全摘要的更早消息，未发生压缩
                logger.info(f"会话{session_id}手动压缩跳过：{before}条均在保留尾部内")
                return {"compacted": False, "total": total, "before": before, "after": before}

            await self._store.add_compaction_mark(
                session_id, build_summary_mark(self._compaction_params, compacted)
            )
            logger.info(f"会话{session_id}手动压缩完成：{before}条 → {len(compacted)}条")
            return {
                "compacted": True,
                "total": total,
                "before": before,
                "after": len(compacted),
                "tokens_before": estimate_context_tokens(messages),
                "tokens_after": estimate_context_tokens(compacted),
                "summary": extract_summary(compacted),
            }
        finally:
            lock.release()

    async def session_stats(self, session_id: str) -> dict[str, Any] | None:
        """会话统计"""
        session_lock = await self._ensure_session_lock(session_id)
        async with session_lock:
            total = await self._store.count(session_id)
            if total == 0:
                return None
            messages = await self._store.restore(session_id, params=self._compaction_params)

            model = self._agent.model
            assert isinstance(model, Model), "Agent 构造时传入的必为 Model 实例"
            stats: dict[str, Any] = {
                "model": model.model_name,
                "context_window": self._compaction_params.context_window
                or resolve_context_window(model),
                "context_tokens": estimate_context_tokens(messages),
                "total_messages": total,
                "sent_messages": len(messages),
            }

            if self._sandbox_manager is not None:
                stats["workspace_files"] = _list_workspace_files(
                    Path(self._sandbox_manager.workspace_root(session_id))
                )
            return stats


chat_manager = ChatSessionManager()
