"""Pydantic AI 模型接入层。

将 `BaseAgentConfig` 转换为 Pydantic AI 的模型实例与 `ModelSettings`，
并按 `(base_url, api_key, headers, model)` 缓存，避免每次运行重建连接池。
"""

import asyncio
import os
from collections.abc import Sequence

from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelResponse
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from kanade_bot.utils.schema import BaseAgentConfig, ProviderConfig

type _ClientKey = tuple[str | None, str | None, tuple[tuple[str, str], ...] | None]
"""客户端缓存键：(base_url, api_key, headers键值对元组)"""

_client_cache: dict[_ClientKey, AsyncOpenAI] = {}
_model_cache: dict[tuple[_ClientKey, str], OpenAIChatModel] = {}


def _client_key(provider: ProviderConfig | None) -> _ClientKey:
    headers = provider.headers if provider else None
    headers_key = tuple(sorted(headers.items())) if headers else None
    return (
        provider.base_url if provider else None,
        provider.api_key if provider else None,
        headers_key,
    )


def _get_client(provider: ProviderConfig | None) -> AsyncOpenAI:
    headers = provider.headers if provider else None

    key = _client_key(provider)
    client = _client_cache.get(key)
    if client is None:
        api_key = (provider.api_key if provider else None) or os.environ.get("OPENAI_API_KEY")
        client = AsyncOpenAI(
            base_url=provider.base_url if provider else None,
            api_key=api_key,
            default_headers=headers,
            max_retries=5,
        )
        _client_cache[key] = client
    return client


def get_model(config: BaseAgentConfig) -> OpenAIChatModel:
    """获取（并缓存）配置对应的 Chat Completions 模型实例"""
    if not config.model:
        raise ValueError("未配置模型ID（config: model）")

    provider = config.provider
    cache_key = (_client_key(provider), config.model)
    model = _model_cache.get(cache_key)
    if model is None:
        model = OpenAIChatModel(
            config.model,
            provider=OpenAIProvider(openai_client=_get_client(provider)),
        )
        _model_cache[cache_key] = model
    return model


def build_model_settings(config: BaseAgentConfig) -> ModelSettings:
    """将配置映射为 `ModelSettings`

    `reasoning_effort` 没有专用字段，走 `extra_body` 透传给 provider。
    """
    extra_body: dict[str, object] = {}
    if config.reasoning_effort:
        extra_body["reasoning_effort"] = config.reasoning_effort

    settings: ModelSettings = {}
    if max_tokens := config.max_output_tokens:
        settings["max_tokens"] = max_tokens
    if extra_body:
        settings["extra_body"] = extra_body
    return settings


# ===== 拼接续写（无会话的一次性调用场景） =====

CONTINUE_PROMPT = "Please continue from where you left off."
"""截断后续写的提示语"""

TRUNCATED = "length"
"""表示输出被 `max_output_tokens` 截断的 `finish_reason`"""


async def run_with_continuation(
    agent: Agent,
    user_prompt: str | Sequence[UserContent],
    *,
    max_requests: int = 1,
    message_history: Sequence[ModelMessage] | None = None,
    timeout: float | None = None,
) -> str:
    """运行并返回完整输出

    截断（`finish_reason == 'length'`）时把已生成内容作为历史回传并请求继续，拼接为完整文本。

    适用于无会话的一次性调用；带会话的流式场景由会话管理器自行实现续写。
    """
    parts: list[str] = []
    history: Sequence[ModelMessage] | None = message_history
    current: str | Sequence[UserContent] = user_prompt

    async def _run_all() -> str:
        nonlocal history, current
        while True:
            result = await agent.run(
                current,
                message_history=history,
                usage_limits=UsageLimits(request_limit=max_requests),
            )
            content = result.output
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("模型没有返回任何输出")
            parts.append(content)

            if (
                (m := result.all_messages()[-1])
                and isinstance(m, ModelResponse)
                and m.finish_reason != TRUNCATED
            ):
                return "".join(parts)

            # 截断：把已生成内容并入历史，从中断处续写
            history = result.all_messages()
            current = CONTINUE_PROMPT

    if timeout:
        async with asyncio.timeout(timeout):
            return await _run_all()
    return await _run_all()
