from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from copilot import CopilotClient
from copilot.client import StopError
from copilot.session import AzureProviderOptions
from httpx import AsyncClient
from nonebot import get_driver, logger
from nonebot.adapters import Event
from nonebot.adapters.console import Event as ConsoleEvent
from nonebot.adapters.onebot.v11 import Event as OneBotEvent

from .schema import AttrDocModel

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


class ProviderConfig(AttrDocModel):
    """自定义API提供商配置

    此模型用于Pydantic校验，运行时通过`.model_dump()`方法获取字典形式的配置。

    修改自`copilot.session.ProviderConfig`，仅保留了可生成schema的字段。
    """

    type: Literal["openai", "azure", "anthropic"] | None = None
    wire_api: Literal["completions", "responses"] | None = None

    transport: Literal["http", "websockets"] | None = None
    """Transport for OpenAI Responses requests. Defaults to "http". Set 
    "websockets" to deliver Responses API requests over a persistent WebSocket
    connection instead of HTTP. Applies to OpenAI-compatible providers using
    wire_api "responses"."""
    base_url: str | None = None
    api_key: str | None = None

    bearer_token: str | None = None
    """Bearer token for authentication. Sets the Authorization header directly.
    Use this for services requiring bearer token auth instead of API key.
    Takes precedence over api_key when both are set."""
    azure: AzureProviderOptions | None = None
    """Azure-specific options"""
    headers: dict[str, str] | None = None

    model_id: str | None = None
    """Well-known model name used by the runtime to look up agent configuration
    (tools, prompts, reasoning behavior) and default token limits. Also used
    as the wire model when wire_model is not set.
    Falls back to SessionConfig.model."""

    wire_model: str | None = None
    """Model name sent to the provider API for inference. Use this when the
    provider's model name (e.g. an Azure deployment name or a custom
    fine-tune name) differs from model_id.
    Falls back to model_id, then SessionConfig.model."""

    max_prompt_tokens: int | None = None
    """Overrides the resolved model's default max prompt tokens. The runtime
    triggers conversation compaction before sending a request when the prompt
    (system message, history, tool definitions, user message) would exceed
    this limit."""

    max_output_tokens: int | None = None
    """Overrides the resolved model's default max output tokens. When hit, the
    model stops generating and returns a truncated response."""


HTTPX_CLIENT = AsyncClient(timeout=20)
"""全局HTTPX客户端单例"""


COPILOT_CLIENT = CopilotClient()
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
