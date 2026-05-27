import asyncio
import sys

from copilot import CopilotClient
from copilot.generated.session_events import SessionEvent, SessionEventType
from copilot.session import PermissionHandler


async def main():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all, model="gpt-4.1", streaming=True
    )

    # Listen for response chunks
    def handle_event(event: SessionEvent):
        if event.type == SessionEventType.ASSISTANT_MESSAGE_DELTA:
            sys.stdout.write(event.data.delta_content)
            sys.stdout.flush()
        if event.type == SessionEventType.SESSION_IDLE:
            print()  # New line when done

    session.on(handle_event)

    await session.send_and_wait("Tell me a short joke")

    await client.stop()


asyncio.run(main())
