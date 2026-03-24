import asyncio
from base64 import b64encode
from encodings.base64_codec import base64_decode, base64_encode
import json
from pathlib import Path
from copilot import BlobAttachment, CopilotClient, PermissionHandler


async def main():
    client = CopilotClient()
    await client.start()

    session = await client.create_session(
        on_permission_request=PermissionHandler.approve_all,
        model="gpt-5-mini",
    )
    response = await session.send_and_wait(
        prompt="描述一下图片",
        attachments=[
            {
                "type": "blob",
                "data": b64encode(Path("宵崎奏Ciallo.webp").read_bytes()).decode(),
                "mimeType": "image/png",  # webp类型不支持，但是可以用image/png代替
                "displayName": "宵崎奏Ciallo.webp",
            }
        ],
    )

    print(response.data.content)
    # models = await client.list_models()
    # # 94.2
    # for model in models:
    #     print(json.dumps(model.to_dict(), ensure_ascii=False, indent=2))

    # await client.stop()


asyncio.run(main())
