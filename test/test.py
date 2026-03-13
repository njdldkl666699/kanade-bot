import asyncio
from copilot import CopilotClient, PermissionHandler


async def main():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        {"model": "gpt-4.1", "on_permission_request": PermissionHandler.approve_all}
    )
    response = await session.send_and_wait({"prompt": "What is 2 + 2?"})

    print(response.data.content)

    await client.stop()


asyncio.run(main())
