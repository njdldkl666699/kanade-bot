import asyncio
from base64 import b64encode
import os
from pathlib import Path
from copilot import CopilotClient
from copilot.session import PermissionHandler
from copilot.generated.session_events import AssistantMessageData
from dotenv import load_dotenv

load_dotenv(Path(".env.prod"))
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
if not OPENROUTER_API_KEY:
    raise ValueError("未找到环境变量 OPENROUTER_API_KEY .env.prod 文件")


async def main():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        model="moonshotai/kimi-k2.6:free",
        provider={
            "type": "openai",
            "api_key": OPENROUTER_API_KEY,
            "base_url": "https://openrouter.ai/api/v1",
        },
    )
    response = await session.send_and_wait(
        prompt="你好，请介绍一下你自己。",
        # attachments=[
        #     {
        #         "type": "blob",
        #         "data": b64encode(Path("Ciallo.webp").read_bytes()).decode(),
        #         "mimeType": "image/png",  # webp类型不支持，但是可以用image/png代替
        #         "displayName": "Ciallo.webp",
        #     }
        # ],
    )

    if response is None:
        print("未收到响应")
        return

    match response.data:
        case AssistantMessageData() as msg:
            print("助手回复了消息：", msg.content)

    # models = await client.list_models()
    # # 94.2
    # for model in models:
    #     print(json.dumps(model.to_dict(), ensure_ascii=False, indent=2))

    # await client.stop()


asyncio.run(main())
