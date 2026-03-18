import asyncio
import json
from copilot import CopilotClient, PermissionHandler


async def main():
    client = CopilotClient()
    await client.start()

    # session = await client.create_session(
    #     {"model": "gpt-4.1", "on_permission_request": PermissionHandler.approve_all}
    # )
    # response = await session.send_and_wait({"prompt": "What is 2 + 2?"})

    # print(response.data.content)
    models = await client.list_models()
    # 94.2
    for model in models:
        print(json.dumps(model.to_dict(), ensure_ascii=False, indent=2))

    await client.stop()


asyncio.run(main())
