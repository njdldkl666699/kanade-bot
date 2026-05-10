from httpx import AsyncClient
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)


client = AsyncClient(
    headers={"Content-Type": "application/json;charset=utf-8"},
    base_url="https://sctapi.ftqq.com",
)


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
    if not (key := cfg.help_server_chan_turbo_key):
        logger.warning("未配置 Server酱Turbo 的Key，无法发送Bot掉线通知")
        return

    # 构建消息主体
    title = f"[KanadeBot] 你的Bot掉线了 {tag}"
    content = f"你的Bot账号: {bot_id} 掉线了，赶快去看看吧。\n`Message`: {message}"

    # 发送通知
    try:
        response = await client.post(
            f"/{key}.send",
            json={
                "title": title,
                "desp": content,
                "channel": 9,  # 方糖服务号
            },
        )
    except Exception as e:
        logger.error(f"发送Bot掉线通知请求时发生异常: {e}")
        return

    if response.status_code == 200:
        data = response.json()
        if data.get("errcode") == 0:
            logger.info("成功发送Bot掉线通知")
            return

    logger.error(f"发送Bot掉线通知失败，响应: {response}")


driver = get_driver()


@driver.on_startup
async def _():
    logger.info("Server酱Turbo HTTP客户端已启动")


@driver.on_shutdown
async def _():
    await client.aclose()
    logger.info("Server酱Turbo HTTP客户端已关闭")
