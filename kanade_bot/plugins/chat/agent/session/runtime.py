"""单会话运行时对象：消息缓冲区、通知槽、流中断与 Agent 轮次编排"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from nonebot import logger
from pydantic_ai import (
    AgentRunEvents,
    FunctionToolCallEvent,
    ModelMessage,
    ModelResponse,
    PartEndEvent,
    RunUsage,
    TextPart,
    UsageLimits,
    UserContent,
)
from pydantic_ai.models import Model
from pydantic_ai_harness.compaction import (
    compact_now,
    estimate_context_tokens,
    resolve_context_window,
)

from kanade_bot.utils.billing import UsageCallback
from kanade_bot.utils.pai_runtime import CONTINUE_PROMPT
from kanade_bot.utils.parse import ImageInput
from kanade_bot.utils.session import SessionInfo

from ...config import cfg
from ..deps import ChatDeps
from ..history import (
    build_summary,
    build_summary_mark,
    compact_between_turns,
    estimate_post_compaction_tokens,
    extract_summary,
    persist_run,
)
from ..memory import MemoryContext
from .input_build import build_send_prompt, build_user_content, stage_images_to_sandbox
from .stats import count_emptied_messages, list_workspace_files

if TYPE_CHECKING:
    from ..manager import ChatSessionManager

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


class EmptyResponseError(RuntimeError):
    """重发耗尽后模型仍返回空响应

    抛出前已回退本轮写入的消息
    """


class ChatSession:
    """单个聊天会话的运行时状态与轮次编排"""

    def __init__(self, session_id: str, *, manager: ChatSessionManager):
        self.session_id = session_id
        self._mgr = manager
        """所属共享运行时"""

        self.lock = asyncio.Lock()
        """会话锁：串行化同一会话的轮次运行与状态修改"""

        self.buffer: deque[str] = deque(maxlen=cfg.session.buffer_max_size)
        """消息缓冲区：暂存尚未发送到模型的消息"""

        self.notification: str | None = None
        """待注入下一轮提示词的系统通知"""

        self.current_stream: AgentRunEvents[str] | None = None
        """进行中的流式运行，用于中断"""

        self.memory_context: MemoryContext | None = None
        """最近发送者的记忆作用域上下文"""

        self._cache_read_total: int = 0
        """会话级缓存命中 token 累计"""

        self._input_total: int = 0
        """会话级输入 token 累计"""

        self.on_buffer_consumed: Callable[[str], None] | None = None
        """buffer 被轮次消费后的回调，函数参数为 session_id，None 表示未启用

        主动回复运行时注入：被动回复消费 buffer 后重置防抖批次；
        同步轻操作，在会话锁内调用。reset 丢弃 buffer 不回调。
        """

    # ===== 缓冲区与通知 =====

    async def add_message(self, prompt: str):
        """向缓冲区添加消息"""
        async with self.lock:
            # deque(maxlen)会在溢出时自动丢弃最早的消息
            self.buffer.append(prompt)

    async def add_system_notification(self, notification: str):
        """添加系统通知，将在下一次发送消息时附加到提示词中"""
        async with self.lock:
            self.notification = notification

    # ===== 记忆上下文 =====

    def update_memory_context(self, session_info: SessionInfo) -> MemoryContext:
        """更新（或首次建立）当前发送者的记忆作用域上下文"""
        if self.memory_context is None:
            self.memory_context = MemoryContext.from_session_info(session_info)
        else:
            self.memory_context.update(session_info)
        return self.memory_context

    # ===== 发送与流式 =====

    async def send_and_wait(
        self,
        session_info: SessionInfo,
        prompt: str,
        bot_id: str,
        *,
        rag_docs: list[str] | None = None,
        reply_text: str | None = None,
        images: list[ImageInput] | None = None,
        timeout: float = 60,
        on_usage: UsageCallback | None = None,
        system_notification: str | None = None,
        require_buffer: bool = False,
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
        require_buffer: 取得会话锁后复检缓冲区，为空则直接放弃本轮（空生成器）。
            主动回复执行器专用：外层的 buffer 非空检查与真正开始轮次之间，被动轮
            （to_me 触发）可能恰好消费了同一批消息，此时无新内容可接话，放弃才能
            避免对同一批消息连回两次。定时任务唤醒不设此项（通知本身即内容）。
        """
        mgr = self._mgr
        session_id = self.session_id
        async with self.lock:
            if require_buffer and not self.buffer:
                # 防重判（会话锁内）：前置检查时 buffer 非空，但等锁期间已被并发轮次
                # 消费。继续接话会对同一批消息重复回复，直接放弃
                logger.info(f"主动回复轮开始时缓冲区已被并发轮次消费，跳过会话{session_id}")
                return

            # Group sessions are shared by members; switch the tool context to
            # the current sender while the per-session lock is held.
            memory_context = self.update_memory_context(session_info)

            if system_notification is not None:
                # 显式注入：不消费排队槽位，排队通知留待下一轮
                notice = system_notification
            else:
                # 将系统通知附加到提示词中
                notice, self.notification = self.notification, None
            if not prompt and not self.buffer and not reply_text and not images and not notice:
                # 没有任何新的消息可发送，直接返回（空生成器）
                logger.info("发送给模型的消息为空，未触发生成")
                return

            send_prompt = build_send_prompt(
                session_info,
                prompt,
                rag_docs=rag_docs,
                messages=self.buffer,
                reply_text=reply_text,
                system_notification=notice,
            )

            # 沙箱启用时获取会话常驻沙箱（首次使用时创建，跨轮保留）
            sandbox_session = None
            sandbox_root = None
            staged_images: list[str] = []
            if mgr.sandbox_manager is not None:
                sandbox_session = await mgr.sandbox_manager.create(session_id)
                sandbox_root = mgr.sandbox_manager.workspace_root(session_id)
                # 用户图片写入沙箱工作区
                staged_images = await stage_images_to_sandbox(sandbox_session, images)
            if staged_images:
                send_prompt += (
                    "\n\n$ 本轮用户发送的图片已保存到沙箱工作区"
                    "（工具可用这些相对路径访问）：\n" + "\n".join(f"- {p}" for p in staged_images)
                )

            user_content = await build_user_content(send_prompt, images)

            deps = ChatDeps(
                session_info=session_info,
                bot_id=bot_id,
                memory_context=memory_context,
                memory_store=mgr.memory_store,
                sandbox=sandbox_session,
                sandbox_root=sandbox_root,
            )

            # 恢复历史：全量原始消息 + 按 marks 重放压缩
            history = await mgr.store.restore(session_id, params=mgr.compaction_params)

            try:
                total_usage = RunUsage()
                for attempt in range(1 + EMPTY_RESPONSE_MAX_RETRIES):
                    # 记录本轮写入前的基准：消息空间（历史回退点）与行空间（DB 回退点）。
                    # 存在压缩时两者不相等，不能混用（见 SessionStore.rollback）
                    baseline = len(history)
                    db_floor = await mgr.store.count(session_id)
                    produced = False
                    tool_called = False
                    continuations = 0

                    current_input: str | list[UserContent] = user_content
                    while True:
                        run: list[ModelMessage] | None = None
                        events = mgr.agent.run_stream_events(
                            current_input,
                            message_history=history,
                            deps=deps,
                            usage_limits=UsageLimits(request_limit=REQUEST_LIMIT),
                            toolsets=mgr.mcp_toolsets or None,
                        )
                        async with events as stream:
                            self.current_stream = stream
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
                                self.current_stream = None

                            run = list(stream.all_messages())
                            run_new = stream.new_messages()

                        # 持久化本轮新增消息与压缩事件。必须用 all_messages（含压缩后真实历史）
                        # run 内发生过压缩时 new_messages() 的切片基准已失效，
                        # 「restore 结果 + new_messages」会丢掉压缩效果
                        await persist_run(
                            mgr.store,
                            session_id,
                            mgr.compaction,
                            base=history,
                            all_messages=run,
                            new_messages=run_new,
                        )
                        history = run
                        # 累计本次运行的usage（含空响应重发与续写）
                        total_usage.incr(stream.usage)
                        # 会话级缓存命中累计：平均命中率 = cache_read总量 / 输入总量（Token加权）
                        self._input_total += stream.usage.input_tokens
                        self._cache_read_total += stream.usage.cache_read_tokens

                        # 输出因 max_output_tokens 截断：续写历史已入库，续写输入只需一句提示
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

                    if produced or tool_called:
                        if on_usage is not None:
                            on_usage(total_usage, produced)
                        return

                    # 整轮空响应：回退本轮写入的消息后重发
                    history = history[:baseline]
                    await mgr.store.rollback(session_id, db_floor)
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
                # 常驻沙箱不在此处关闭：跨轮保留 shell 会话状态，仅在会话重置或进程退出时关闭
                # 丢弃未消费的压缩事件（超时/取消路径残留），避免污染下一轮
                mgr.compaction.take_events(session_id)
                # 轮末自动压缩：最终回复已落库、下一轮开始前执行，不会打断任何进行中的任务
                try:
                    model = mgr.agent.model
                    assert isinstance(model, Model), "Agent 构造时传入的必为 Model 实例"
                    if info := await compact_between_turns(
                        mgr.store,
                        session_id,
                        mgr.compaction_params,
                        history,
                        model=model,
                        model_settings=mgr.model_settings,
                        output_upper_tokens=mgr.output_upper_tokens,
                    ):
                        logger.info(f"会话{session_id}轮末压缩完成：{info}")
                except Exception as e:
                    logger.warning(f"会话{session_id}轮末压缩失败（不影响会话状态）：{e}")
                # 清空消息缓冲区
                self.buffer.clear()
                if (callback := self.on_buffer_consumed) is not None:
                    callback(session_id)

    # ===== 管理操作 =====

    def interrupt(self) -> bool | None:
        """手动中断当前正在运行的回复，不影响后续消息

        未在生成中时返回 None；中断成功返回 True。
        """
        stream = self.current_stream
        if stream is None:
            return None
        stream.cancel()
        return True

    async def reset(self):
        """清空会话历史、缓冲区、记忆上下文并关闭沙箱。**此操作不可逆**"""
        mgr = self._mgr
        async with self.lock:
            self.buffer.clear()
            self.notification = None
            self.memory_context = None
            self._cache_read_total = 0
            self._input_total = 0

            try:
                await mgr.store.clear(self.session_id)
            except Exception as e:
                logger.warning(f"清空会话{self.session_id}历史时发生错误: {e}")

            if mgr.sandbox_manager is not None:
                await mgr.sandbox_manager.close(self.session_id)

    async def clear_workspace(self) -> bool | None:
        """关闭会话沙箱并删除工作区文件。**此操作不可逆**

        沙箱未启用时返回 None；工作区目录不存在（无内容可删）返回 False；
        删除成功返回 True。会话历史不受影响。
        """
        mgr = self._mgr
        if mgr.sandbox_manager is None:
            return None
        workspace_root = Path(mgr.sandbox_manager.workspace_root(self.session_id))
        existed = workspace_root.is_dir()
        async with self.lock:
            await mgr.sandbox_manager.close(self.session_id)
            mgr.sandbox_manager.delete_workspace(self.session_id)
        return existed

    async def compact(self) -> dict[str, Any] | None:
        """手动执行一次 LLM 总结级压缩"""
        mgr = self._mgr
        try:
            async with asyncio.timeout(COMPACT_LOCK_TIMEOUT):
                await self.lock.acquire()
        except TimeoutError:
            raise RuntimeError(
                f"会话正在处理中（{COMPACT_LOCK_TIMEOUT}s内未获得会话锁），可先使用 /中断会话"
            ) from None

        try:
            total = await mgr.store.count(self.session_id)
            if total == 0:
                return None

            messages = await mgr.store.restore(self.session_id, params=mgr.compaction_params)
            before = len(messages)
            model = mgr.agent.model
            assert isinstance(model, Model), "Agent 构造时传入的必为 Model 实例"

            compacted = await compact_now(
                build_summary(
                    mgr.compaction_params,
                    model_settings=mgr.model_settings,
                    output_upper_tokens=mgr.output_upper_tokens,
                ),
                messages,
                model=model,
                conversation_id=self.session_id,
            )
            if compacted is messages:
                # 历史全落在保留尾部内：无可安全摘要的更早消息，未发生压缩
                logger.info(f"会话{self.session_id}手动压缩跳过：{before}条均在保留尾部内")
                return {"compacted": False, "total": total, "before": before, "after": before}

            mark = build_summary_mark(mgr.compaction_params, compacted)
            mark.up_to_seq = total - 1
            await mgr.store.add_compaction_mark(self.session_id, mark)
            est = estimate_context_tokens(messages)
            logger.info(f"会话{self.session_id}手动压缩完成：{before}条 → {len(compacted)}条")
            return {
                "compacted": True,
                "total": total,
                "before": before,
                "after": len(compacted),
                "tokens_before": est,
                "tokens_after": estimate_post_compaction_tokens(messages, compacted, baseline=est),
                "summary": extract_summary(compacted),
            }
        finally:
            self.lock.release()

    async def stats(self) -> dict[str, Any] | None:
        """会话统计"""
        mgr = self._mgr
        async with self.lock:
            total = await mgr.store.count(self.session_id)
            if total == 0:
                return None
            messages = await mgr.store.restore(self.session_id, params=mgr.compaction_params)

            model = mgr.agent.model
            assert isinstance(model, Model), "Agent 构造时传入的必为 Model 实例"
            stats: dict[str, Any] = {
                "model": model.model_name,
                "context_window": mgr.compaction_params.context_window
                or resolve_context_window(model),
                "context_tokens": estimate_context_tokens(messages),
                "total_messages": total,
                "sent_messages": len(messages),
            }
            # 展示口径：剔除内容已全部清空的空壳消息（实际请求仍包含它们）
            if emptied := count_emptied_messages(messages):
                stats["emptied_messages"] = emptied

            # 平均缓存命中率：会话级累计值（Token加权），内存态、重启后重新计算
            if self._input_total > 0:
                stats["cache"] = {
                    "hit_rate": self._cache_read_total / self._input_total,
                    "cache_read_tokens": self._cache_read_total,
                    "input_tokens": self._input_total,
                }

            # restore 之后读取：参数漂移时 restore 会重写 marks，之后才是稳定状态
            if marks := await mgr.store.load_compaction_marks(self.session_id):
                stats["compaction"] = {
                    "marks": len(marks),
                    "latest_strategy": marks[-1].strategy,
                    "latest_at": marks[-1].applied_at,
                }

            if mgr.sandbox_manager is not None:
                stats["workspace_files"] = list_workspace_files(
                    Path(mgr.sandbox_manager.workspace_root(self.session_id))
                )
            return stats
