import json
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from copilot import CopilotClient
from copilot.client import StopError
from nonebot import get_driver, logger
from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.console.event import PublicMessageEvent as ConsolePublicMessageEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent as OneBotGroupMessageEvent
from nonebot.adapters.onebot.v11 import PrivateMessageEvent as OneBotPrivateMessageEvent
from nonebot.params import EventToMe
from pydantic import BaseModel

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


def group_permission(event: OneBotGroupMessageEvent | ConsolePublicMessageEvent) -> bool:
    """匹配群聊消息类型事件"""
    return True


def superuser_onebot_private_permission(event: OneBotPrivateMessageEvent) -> bool:
    """匹配OneBot私聊消息类型事件且发送者是超级用户"""
    return event.get_user_id() in get_driver().config.superusers


def not_to_me(to_me: bool = EventToMe()):
    """匹配与机器人无关的消息"""
    return not to_me


def asia_shanghai_now() -> datetime:
    """获取当前的上海时间"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


class AttrDocModel(BaseModel):
    """带有属性docstring的Pydantic模型基类"""

    model_config = {
        "use_attribute_docstrings": True,
    }


driver = get_driver()


def generate_schema[T: BaseModel](cls: type[T]):
    """生成JSON Schema文件"""
    if not driver.config.generate_schemas:
        return

    schema_file_name = f"{cls.__name__}.json"
    logger.info(f"正在生成JSON Schema文件: {schema_file_name}")
    json_schema = json.dumps(cls.model_json_schema(), indent=2, ensure_ascii=False)
    schema_file = Path("schemas") / "generated" / schema_file_name
    schema_file.parent.mkdir(parents=True, exist_ok=True)
    schema_file.write_text(json_schema, encoding="utf-8")


COPILOT_CLIENT = CopilotClient()
"""全局Copilot客户端单例

负责与Copilot服务进行通信，创建和恢复会话等操作
"""


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
