import os
from email.header import Header
from pathlib import Path

import httpx
from dotenv import load_dotenv
from loguru import logger

load_dotenv(".env.prod")


def main():
    if not (url := os.getenv("HELP__NTFY_TOPIC_URL")):
        logger.warning("未配置ntfy topic url，无法发送Bot掉线通知")
        return

    # 构建消息主体
    title = "测试 你的Bot掉线了"
    content = "你的Bot账号: 测试 掉线了，赶快去看看吧。\n`Message`: 测试".encode()
    encoded_title = Header(title, "utf-8").encode()
    headers = {"Title": encoded_title}

    path = Path("Ciallo.webp")
    if path and path.is_file():
        content = path.read_bytes()
        headers["Filename"] = path.name

    # 发送通知
    try:
        response = httpx.put(
            url,
            headers=headers,
            content=content,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as e:
        logger.error(f"发送Bot掉线通知请求时发生异常: {e}")
        return


main()
