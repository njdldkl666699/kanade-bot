from httpx import AsyncClient
from nonebot import get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config).help


client = AsyncClient()


async def send_offline_notice(
    bot_id: int,
    tag: str,
    message: str,
):
    """发送通知消息

    :param bot_id: 掉线的Bot账号
    :param tag: 通知事件的标签
    :param message: 通知消息内容
    """
    if not (url := cfg.ntfy_topic_url):
        logger.warning("未配置ntfy topic url，无法发送Bot掉线通知")
        return

    # 构建消息主体
    title = f"{tag} 你的Bot掉线了"
    content = f"你的Bot账号: {bot_id} 掉线了，赶快去看看吧。\n`Message`: {message}".encode("utf-8")
    headers = {"Title": title}

    path = cfg.login_qrcode_file_path
    if path and path.is_file():
        content = path.read_bytes()
        headers["Filename"] = path.name

    # 发送通知
    try:
        response = await client.put(
            url,
            headers=headers,
            content=content,
        )
        response.raise_for_status()
    except Exception as e:
        logger.error(f"发送Bot掉线通知请求时发生异常: {e}")
        return
