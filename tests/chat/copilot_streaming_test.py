import asyncio
import sys
from asyncio import Queue

from copilot import CopilotClient, CopilotSession
from copilot.generated.session_events import (
    AssistantMessageDeltaData,
    SessionErrorData,
    SessionEvent,
    SessionIdleData,
)
from copilot.session import PermissionHandler


async def collect_stream_response(
    session: CopilotSession,
    prompt: str,
) -> str:
    """发送消息并收集完整的流式响应"""
    stream_queue: Queue[str | None] = Queue()
    stream_error = None

    def handle_event(event: SessionEvent):
        nonlocal stream_error

        match event.data:
            case AssistantMessageDeltaData() as delta:
                stream_queue.put_nowait(
                    delta.delta_content
                )  # delta_content的类型是str，不可能为None
            case SessionIdleData():
                stream_queue.put_nowait(None)  # 发送None表示完成
            case SessionErrorData() as data:
                stream_error = Exception(f"Session error: {data.message or str(data)}")
                stream_queue.put_nowait(None)  # 发送None表示完成

    session.on(handle_event)

    # 发送消息
    await session.send(prompt)

    # 等待完成事件，同时实时输出
    chunks = []
    while True:
        chunk = await stream_queue.get()

        if chunk is None:
            break

        chunks.append(chunk)
        sys.stdout.write(chunk)
        sys.stdout.flush()

    if stream_error:
        raise stream_error

    return "".join(chunks)


async def test():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all, model="gpt-4.1", streaming=True
    )

    try:
        await collect_stream_response(session, "Tell me a short joke")
    finally:
        await session.disconnect()
        await client.stop()


if __name__ == "__main__":
    asyncio.run(test())
