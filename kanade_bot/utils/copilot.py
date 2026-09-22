import asyncio
import logging
import time
from typing import Literal

from copilot import CopilotClient, CopilotSession, SessionEvent, StopError
from copilot._diagnostics import log_timing
from copilot.session import Attachment
from copilot.session import logger as copilot_logger
from copilot.session_events import AssistantMessageData, SessionErrorData, SessionIdleData
from nonebot import get_driver, logger

from kanade_bot.utils.common import get_project_version


class SessionStreamError(Exception):
    """会话在流式响应期间报告了SessionErrorData"""


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
):
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
    收到 SessionErrorData 时立即抛出异常，不再等待后续的 idle 事件。

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
        await session.send(
            prompt,
            attachments=attachments,
            mode=mode,
            agent_mode=agent_mode,
            request_headers=request_headers,
            display_prompt=display_prompt,
        )
        first_assistant_message_logged = False
        while True:
            # 每收到一个事件，超时计时就会重置
            try:
                event = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
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
                    raise SessionStreamError(f"Session error: {data.message or str(data)}")
                case SessionIdleData():
                    log_timing(
                        copilot_logger,
                        logging.DEBUG,
                        "copilot_send_and_wait_stream idle received",
                        total_start,
                        session_id=session.session_id,
                    )
                    return
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
    """
    total_start = time.perf_counter()
    idle_event = asyncio.Event()
    error_event: Exception | None = None
    contents: list[str] = []
    first_assistant_message_logged = False

    def handler(event: SessionEvent) -> None:
        nonlocal first_assistant_message_logged, error_event
        match event.data:
            case AssistantMessageData() as data:
                contents.append(data.content)
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
        log_timing(
            copilot_logger,
            logging.DEBUG,
            "copilot_send_and_wait_contents complete",
            total_start,
            session_id=session.session_id,
            completed_by="idle",
            assistant_message_received=bool(contents),
        )
        return contents
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
