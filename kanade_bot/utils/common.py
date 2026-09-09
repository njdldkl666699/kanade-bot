import json
import tomllib
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from copilot import CopilotClient
from copilot.client import StopError
from httpx import AsyncClient, HTTPError
from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent

from .schema import KanadeConfig

type PlatformType = Literal["console", "onebot"]
"""消息平台类型"""


def get_platform_type(event: Event) -> PlatformType:
    """根据事件类型确定消息平台"""
    if isinstance(event, ConsoleEvent):
        return "console"
    elif isinstance(event, OneBotEvent):
        return "onebot"
    else:
        raise TypeError(f"Unsupported event type: {type(event)}")


def asia_shanghai_now() -> datetime:
    """获取当前的上海时间"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


@lru_cache(maxsize=1)
def get_project_version() -> str:
    """获取项目版本号"""
    pyproject_content = Path("pyproject.toml").read_text(encoding="utf-8")
    project_data = tomllib.loads(pyproject_content)
    return project_data["project"]["version"]


HTTPX_CLIENT = AsyncClient(timeout=20)
"""全局HTTPX客户端单例"""


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


QQ_EMOJI_INDEX_URLS = [
    "https://wget.la/https://raw.githubusercontent.com/koishijs/QFace/master/public/assets/qq_emoji/_index.json",
    "https://ghfast.top/https://raw.githubusercontent.com/koishijs/QFace/master/public/assets/qq_emoji/_index.json",
    "https://fastly.jsdelivr.net/gh/koishijs/QFace@master/public/assets/qq_emoji/_index.json",
    "https://raw.githubusercontent.com/koishijs/QFace/master/public/assets/qq_emoji/_index.json",
]


QQ_EMOJI_INDEXES: dict[str, str] = {}
"""QQ表情索引字典，键为表情ID，值为表情描述"""


@driver.on_startup
async def load_qq_emoji_index():
    """在启动时下载QQ表情索引文件并加载"""
    from nonebot_plugin_localstore import BASE_CACHE_DIR

    p = BASE_CACHE_DIR / "qq_emoji_index.json"
    if not p.exists():
        for url in QQ_EMOJI_INDEX_URLS:
            try:
                response = await HTTPX_CLIENT.get(url)
                response.raise_for_status()
                p.write_text(response.text, encoding="utf-8")
                logger.info(f"已下载QQ表情索引文件: {url}")
                break
            except HTTPError as e:
                logger.warning(f"下载QQ表情索引文件失败: {url}, 错误: {e}")
        else:
            logger.error("所有QQ表情索引文件下载失败，请检查网络连接或尝试手动下载")
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for item in data:
                QQ_EMOJI_INDEXES[item["emojiId"]] = item["describe"]
            logger.info("已加载QQ表情索引文件")
        except (json.JSONDecodeError, OSError) as e:
            logger.exception(f"加载QQ表情索引文件失败: {e}")


@driver.on_shutdown
async def clear_image_cache():
    p = get_plugin_config(KanadeConfig).image_cache_dir_path
    if p.exists() and p.is_dir():
        for f in p.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except OSError as e:
                    logger.warning(f"删除图片缓存文件 {f} 时发生错误: {e}")
