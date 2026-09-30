import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from typing import Literal

from copilot import CopilotClient, CopilotSession, SessionEvent, StopError
from copilot._diagnostics import log_timing
from copilot.rpc import AbortRequest, HistoryTruncateRequest
from copilot.session import Attachment
from copilot.session import logger as copilot_logger
from copilot.session_events import (
    AbortReason,
    AssistantMessageData,
    SessionErrorData,
    SessionEventType,
    SessionIdleData,
)
from nonebot import get_driver, logger

from kanade_bot.utils.common import get_project_version


def _patch_provider_wire_conversion() -> None:
    """让SDK的provider wire转换透传 max_context_window_tokens。

    SDK 1.0.14 的 `_convert_provider_to_wire_format` 只转换固定字段列表，
    不含 `max_context_window_tokens`，即使provider dict里带了也会被丢弃；
    而运行时（CLI 1.0.85）恰恰只认BYOK ProviderConfig顶层的
    `maxContextWindowTokens`（`session.open`的`modelCapabilities`参数会被忽略）。
    在创建全局客户端前打补丁，使 `provider.max_context_window_tokens`
    （由`BaseAgentConfig.model_dump_session_config()`自动回填）能够到达运行时。
    升级SDK后若官方已支持（wire转换包含该字段）可移除本补丁。
    """
    original = CopilotClient._convert_provider_to_wire_format

    def convert(self, provider):
        wire = original(self, provider)
        if (mcw := provider.get("max_context_window_tokens")) is not None:
            wire["maxContextWindowTokens"] = mcw
        return wire

    CopilotClient._convert_provider_to_wire_format = convert


_patch_provider_wire_conversion()

COPILOT_CLIENT = CopilotClient(
    # connection=RuntimeConnection.for_inprocess(),
    client_info={
        "application_name": "kanade_bot",
        "application_version": get_project_version(),
    },
)
"""全局Copilot客户端单例

负责与Copilot服务进行通信，创建和恢复会话等操作
"""

driver = get_driver()


@driver.on_startup
async def startup():
    await COPILOT_CLIENT.start()
    logger.info("Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    try:
        await COPILOT_CLIENT.stop()
    except* StopError as eg:
        logger.warning(f"停止Copilot客户端时发生错误: {eg.message}")
    logger.info("Copilot客户端已关闭")


async def abort_session_turn(session: CopilotSession, *, rpc_timeout: float = 30.0) -> bool:
    """中止会话当前正在运行的turn，等同用户主动中断（如CLI的Ctrl+C）。会话空闲时为无操作。

    超时放弃等待后必须调用：仅退订事件监听并不会停止运行时，会话会继续
    生成、继续执行工具（权限审批、发送文件等副作用照常发生），且经
    openai-proxy的上游LLM请求也不会被取消，最终产生迟到的"僵尸回复"，
    并把后续用户消息排队在仍在运行的turn之后。

    返回是否成功中止。
    """
    try:
        result = await session.rpc.abort(
            AbortRequest(reason=AbortReason.USER_INITIATED), timeout=rpc_timeout
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"中止会话{session.session_id}当前turn时发生错误: {e}")
        return False
    if not result.success:
        logger.warning(f"中止会话{session.session_id}当前turn未成功: {result.error}")
        return False
    return True


class EmptyResponseError(RuntimeError):
    """重发耗尽后模型仍返回空响应（content为空字符串且工具调用为空列表）。

    抛出前会尽力截断末轮历史（移除用户消息与空响应），避免历史残留
    空content的assistant消息导致上游后续请求报400。
    """


EMPTY_RESPONSE_MAX_RETRIES = 2
"""空响应（content为空字符串且工具调用为空列表）的最大重发次数，含首发共
1+2次尝试。重发前会先截断本轮历史（连同空响应一起移除），避免历史残留
空content的assistant消息（上游已开始拒绝并报400）以及重复的用户消息。"""


def _is_empty_response(data: AssistantMessageData) -> bool:
    """空响应：content为空字符串且工具调用为空列表（模型把答案写进了
    reasoning_content等错误通道，或未产出任何内容）。"""
    return not data.content.strip() and not data.tool_requests


def _attempt_usable(messages: list[AssistantMessageData]) -> bool:
    """本轮尝试是否产生了可用输出：任一助手消息有内容或发起了工具调用。

    只要本轮发生过工具调用就不重发——重发会重复执行工具副作用（发文件、
    渲染图片等）。"""
    return any(not _is_empty_response(d) for d in messages)


async def _truncate_last_turn(session: CopilotSession) -> bool:
    """截断会话末轮历史：移除最后一个user.message事件及其后的全部事件
    （含空响应），为重发同一请求清理现场。返回是否成功。"""
    try:
        events = await session.get_events()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"读取会话{session.session_id}事件历史失败: {e}")
        return False
    event_id = next(
        (str(e.id) for e in reversed(events) if e.type == SessionEventType.USER_MESSAGE),
        None,
    )
    if not event_id:
        logger.warning(f"会话{session.session_id}历史中未找到user.message事件，无法截断")
        return False
    try:
        result = await session.rpc.history.truncate(
            HistoryTruncateRequest(event_id=event_id), timeout=30
        )
        logger.info(
            f"已截断会话{session.session_id}末轮历史（移除{result.events_removed}个事件，含空响应）"
        )
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"截断会话{session.session_id}末轮历史失败: {e}")
        return False


async def copilot_send_and_wait_stream(
    session: CopilotSession,
    prompt: str,
    *,
    attachments: list[Attachment] | None = None,
    mode: Literal["enqueue", "immediate"] | None = None,
    agent_mode: Literal["interactive", "plan", "autopilot", "shell"] | None = None,
    request_headers: dict[str, str] | None = None,
    display_prompt: str | None = None,
    timeout: float = 60.0,
) -> AsyncGenerator[AssistantMessageData]:
    """
    发送消息到会话，每条AssistantMessageData一到达就实时yield。

    不同于`CopilotSession.send_and_wait`的只返回最后一个助手消息，
    这个方法会产出本轮对话产生的全部AssistantMessageData。

    跨线程桥接：handler 是注册在 `session.on` 上的同步回调，由 JSON-RPC
    读取线程分发，并不在事件循环线程上。这里借助 `asyncio.Queue` +
    `loop.call_soon_threadsafe` 把事件安全地投递回事件循环，使异步调用方
    能够逐条及时消费，而不是等全部消息收集完后一次性返回。

    注意：调用方必须完整消费本生成器，或使用 `contextlib.aclosing` 包裹，
    否则中途退出时 `finally` 中的 `unsubscribe` 不会执行。

    timeout 语义为相邻事件间的间隔超时：每收到一个事件即重置计时。
    超时后会先调用`abort_session_turn`中止仍在运行的turn（防止超时后
    会话继续生成、执行工具副作用），再抛出TimeoutError。
    收到 SessionErrorData 时立即抛出异常，不再等待后续的 idle 事件。

    空响应重发：整轮结束时若本轮全部助手消息均为空响应（content为空
    字符串且工具调用为空列表，模型推理标记灵活多变故不依赖特定标记），
    先截断本轮历史（移除用户消息与空响应）再重发同一请求，至多
    `EMPTY_RESPONSE_MAX_RETRIES` 次；重试耗尽时仍空则截断本轮历史后
    抛出`EmptyResponseError`交由上层处理。

    参数注释参见`CopilotSession.send_and_wait`。
    """
    total_start = time.perf_counter()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[SessionEvent] = asyncio.Queue()

    def handler(event: SessionEvent) -> None:
        # handler运行在JSON-RPC读取线程上，需线程安全地投递回事件循环
        try:
            loop.call_soon_threadsafe(queue.put_nowait, event)
        except RuntimeError:
            # 事件循环已关闭（如进程关闭期间），事件无处可投递，直接丢弃
            pass

    unsubscribe = session.on(handler)
    try:
        first_assistant_message_logged = False
        for attempt in range(1 + EMPTY_RESPONSE_MAX_RETRIES):
            attempt_messages: list[AssistantMessageData] = []
            await session.send(
                prompt,
                attachments=attachments,
                mode=mode,
                agent_mode=agent_mode,
                request_headers=request_headers,
                display_prompt=display_prompt,
            )
            while True:
                # 每收到一个事件，超时计时就会重置
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    # 超时后必须主动中止当前turn：仅退订事件并不会停止运行时。
                    # abort会让运行时放弃本轮（中断在途LLM请求），整条链路才会停下
                    await abort_session_turn(session)
                    log_timing(
                        copilot_logger,
                        logging.WARNING,
                        "copilot_send_and_wait_stream failed",
                        total_start,
                        session_id=session.session_id,
                        completed_by="timeout",
                    )
                    raise TimeoutError(f"Timeout after {timeout}s waiting for session events")
                match event.data:
                    case AssistantMessageData() as data:
                        if not first_assistant_message_logged:
                            first_assistant_message_logged = True
                            log_timing(
                                copilot_logger,
                                logging.DEBUG,
                                "copilot_send_and_wait_stream first assistant message",
                                total_start,
                                session_id=session.session_id,
                            )
                        attempt_messages.append(data)
                        # 空消息不产出（消费方本就会跳过），非空内容实时流式产出
                        if data.content.strip():
                            yield data
                    case SessionErrorData() as data:
                        log_timing(
                            copilot_logger,
                            logging.WARNING,
                            "copilot_send_and_wait_stream failed",
                            total_start,
                            session_id=session.session_id,
                            completed_by="error",
                        )
                        raise RuntimeError(f"Session error: {data.message or str(data)}")
                    case SessionIdleData():
                        log_timing(
                            copilot_logger,
                            logging.DEBUG,
                            "copilot_send_and_wait_stream idle received",
                            total_start,
                            session_id=session.session_id,
                        )
                        if _attempt_usable(attempt_messages):
                            return
                        # 整轮空响应：截断本轮（移除用户消息与空响应）后重发
                        if attempt < EMPTY_RESPONSE_MAX_RETRIES and await _truncate_last_turn(
                            session
                        ):
                            logger.warning(
                                f"会话{session.session_id}本轮回复为空"
                                f"（尝试{attempt + 1}/{1 + EMPTY_RESPONSE_MAX_RETRIES}），"
                                f"已移除空响应并重发"
                            )
                            break
                        # 重试耗尽或截断失败：清理历史后抛给上层处理
                        await _truncate_last_turn(session)
                        raise EmptyResponseError(
                            f"会话{session.session_id}连续{attempt + 1}次回复为空"
                            f"（content为空且无工具调用），已停止重试"
                        )
    finally:
        unsubscribe()


async def copilot_send_and_wait_contents(
    session: CopilotSession,
    prompt: str,
    *,
    timeout: float = 120.0,
) -> list[str]:
    """发送消息到会话，等待完成后返回全部助手消息内容。

    不同于`CopilotSession.send_and_wait`的只返回最后一个助手消息，
    这个方法会收集本轮对话产生的全部AssistantMessageData内容。

    handler 是注册在 `session.on` 上的同步回调，由 JSON-RPC 读取线程分发，
    并不在事件循环线程上；这里只做收集与事件置位，无需流式跨线程桥接。

    空响应重发：整轮结束时若本轮全部助手消息均为空响应（content为空
    字符串且工具调用为空列表），先截断本轮历史（移除用户消息与空响应）
    再重发同一请求，至多`EMPTY_RESPONSE_MAX_RETRIES`次；重试耗尽时仍空
    则截断本轮历史后抛出`EmptyResponseError`交由上层处理。
    """
    total_start = time.perf_counter()
    error_event: Exception | None = None
    first_assistant_message_logged = False
    idle_event = asyncio.Event()
    # 每次尝试整体重绑，handler闭包按名字解析到最新列表/事件
    attempt_messages: list[AssistantMessageData] = []

    def handler(event: SessionEvent) -> None:
        nonlocal first_assistant_message_logged, error_event
        match event.data:
            case AssistantMessageData() as data:
                attempt_messages.append(data)
                if not first_assistant_message_logged:
                    first_assistant_message_logged = True
                    log_timing(
                        copilot_logger,
                        logging.DEBUG,
                        "copilot_send_and_wait_contents first assistant message",
                        total_start,
                        session_id=session.session_id,
                    )
            case SessionIdleData():
                log_timing(
                    copilot_logger,
                    logging.DEBUG,
                    "copilot_send_and_wait_contents idle received",
                    total_start,
                    session_id=session.session_id,
                )
                idle_event.set()
            case SessionErrorData() as data:
                error_event = RuntimeError(f"Session error: {data.message or str(data)}")
                idle_event.set()

    unsubscribe = session.on(handler)
    try:
        for attempt in range(1 + EMPTY_RESPONSE_MAX_RETRIES):
            idle_event = asyncio.Event()
            attempt_messages = []
            await session.send(prompt)
            await asyncio.wait_for(idle_event.wait(), timeout=timeout)
            if error_event:
                log_timing(
                    copilot_logger,
                    logging.WARNING,
                    "copilot_send_and_wait_contents failed",
                    total_start,
                    session_id=session.session_id,
                    completed_by="error",
                )
                raise error_event
            if _attempt_usable(attempt_messages):
                break
            # 整轮空响应：截断本轮（移除用户消息与空响应）后重发
            if attempt < EMPTY_RESPONSE_MAX_RETRIES and await _truncate_last_turn(session):
                logger.warning(
                    f"会话{session.session_id}本轮回复为空"
                    f"（尝试{attempt + 1}/{1 + EMPTY_RESPONSE_MAX_RETRIES}），"
                    f"已移除空响应并重发"
                )
                continue
            # 重试耗尽或截断失败：清理历史后抛给上层处理
            await _truncate_last_turn(session)
            raise EmptyResponseError(
                f"会话{session.session_id}连续{attempt + 1}次回复为空"
                f"（content为空且无工具调用），已停止重试"
            )
        log_timing(
            copilot_logger,
            logging.DEBUG,
            "copilot_send_and_wait_contents complete",
            total_start,
            session_id=session.session_id,
            completed_by="idle",
            assistant_message_received=bool(attempt_messages),
        )
        return [d.content for d in attempt_messages if d.content.strip()]
    except TimeoutError:
        log_timing(
            copilot_logger,
            logging.WARNING,
            "copilot_send_and_wait_contents failed",
            total_start,
            session_id=session.session_id,
            completed_by="timeout",
        )
        raise TimeoutError(f"Timeout after {timeout}s waiting for session.idle")
    finally:
        unsubscribe()
