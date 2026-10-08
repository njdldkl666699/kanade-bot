"""Pydantic AI 模型接入层。

将`BaseAgentConfig`转换为Pydantic AI的模型实例与`ModelSettings`并缓存，避免每次运行重建连接池。
"""

import asyncio
import os
from collections.abc import Sequence

import httpx
from openai import AsyncOpenAI
from pydantic_ai import Agent, ModelResponse
from pydantic_ai.messages import ModelMessage, UserContent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage, UsageLimits

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
    """获取配置对应的 Chat Completions 模型实例"""
    if not config.model:
        raise ValueError("未配置模型ID")

    provider = config.provider
    cache_key = (_client_key(provider), config.model)
    model = _model_cache.get(cache_key)
    if model is None:
        # pydantic-ai 默认把 max_tokens 设置映射为 max_completion_tokens 发送，
        # DeepSeek 等兼容端点只认 max_tokens（未知字段被静默忽略，输出限制失效），
        # 按端点能力声明覆盖映射；未配置 provider（OpenAI 官方端点）保持默认
        profile_kwargs: dict = {}
        if provider is not None:
            profile_kwargs["openai_chat_supports_max_completion_tokens"] = (
                provider.supports_max_completion_tokens
            )

        # 上下文窗口优先级：显式配置 > /models 端点扩展字段 > genai-prices 快照回填。
        # 注意：仅在拿到值时才传字段——profile 合并按 fields_set 区分，显式传 None 会阻止快照回填
        window = config.context_window
        if window is None and provider is not None:
            window, _ = _fetch_model_metadata(provider, config.model)
        if window is not None:
            profile_kwargs["context_window"] = window

        profile = OpenAIModelProfile(**profile_kwargs) if profile_kwargs else None
        model = OpenAIChatModel(
            config.model,
            provider=OpenAIProvider(openai_client=_get_client(provider)),
            profile=profile,
        )
        _model_cache[cache_key] = model

    return model


MODELS_FETCH_TIMEOUT = 5.0
"""向 /models 端点查询模型元数据的超时秒数"""

_models_metadata_cache: dict[tuple[_ClientKey, str], tuple[int | None, int | None]] = {}
"""(client, model) → (context_window, max_output_tokens)，/models 查询结果缓存"""


def _positive(entry: dict, *keys: str) -> int | None:
    for key in keys:
        value = entry.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return None


def _extract_model_metadata(payload: dict, model: str) -> tuple[int | None, int | None]:
    """从 /models 响应中提取指定模型的窗口与最大输出 token 数

    兼容两种字段命名：
    - DeepSeek 风格：`context_window` / `max_output_tokens`
    - OpenRouter 风格：`context_length` / `max_output_length`
    """

    for entry in payload.get("data", []):
        if not isinstance(entry, dict) or entry.get("id") != model:
            continue
        return (
            _positive(entry, "context_window", "context_length"),
            _positive(entry, "max_output_tokens", "max_output_length"),
        )
    return None, None


def _fetch_model_metadata(provider: ProviderConfig, model: str) -> tuple[int | None, int | None]:
    """从 OpenAI 兼容端点的 /models 响应获取模型元数据

    :return: `(context_window, max_output_tokens)`，若无法获取则返回 `(None, None)`。
    """

    key = (_client_key(provider), model)
    if key in _models_metadata_cache:
        return _models_metadata_cache[key]

    metadata = (None, None)
    if provider.base_url:
        api_key = provider.api_key or os.environ.get("OPENAI_API_KEY")
        headers = {"Authorization": f"Bearer {api_key}", **(provider.headers or {})}
        url = f"{provider.base_url.rstrip('/')}/models"
        try:
            resp = httpx.get(url, headers=headers, timeout=MODELS_FETCH_TIMEOUT)
            resp.raise_for_status()
            metadata = _extract_model_metadata(resp.json(), model)
        except Exception:  # noqa: S110
            pass
    _models_metadata_cache[key] = metadata
    return metadata


def resolve_model_context_window(config: BaseAgentConfig) -> int | None:
    """解析模型实际生效的上下文窗口：显式配置 > /models 元数据 > genai-prices

    与压缩能力的每请求解析同源（优先 profile、再 registry），但**只解析一次**
    供调用方固化使用——避免 registry/网络状态随进程变化导致压缩阈值漂移。
    `_fetch_model_metadata` 失败不缓存，这里调用会多一次重试机会；
    仍拿不到时返回 None（调用方保持未固化状态，压缩能力自行回退）。
    """

    if config.context_window:
        return config.context_window

    provider = config.provider
    if provider is not None and (window := _fetch_model_metadata(provider, config.model)[0]):
        return window

    from pydantic_ai_harness.compaction import resolve_context_window

    return resolve_context_window(get_model(config))


def build_model_settings(config: BaseAgentConfig) -> ModelSettings:
    """将配置映射为 `ModelSettings`"""
    extra_body: dict[str, object] = {}
    if config.reasoning_effort:
        extra_body["reasoning_effort"] = config.reasoning_effort

    max_tokens = config.max_output_tokens
    if max_tokens is None and config.provider is not None:
        _, max_tokens = _fetch_model_metadata(config.provider, config.model)

    settings: ModelSettings = {}
    if max_tokens:
        settings["max_tokens"] = max_tokens
    if extra_body:
        settings["extra_body"] = extra_body
    return settings


# ===== 拼接续写 =====

CONTINUE_PROMPT = "Please continue from where you left off."
"""截断后续写的提示语"""


async def run_with_continuation(
    agent: Agent,
    user_prompt: str | Sequence[UserContent],
    *,
    max_requests: int = 1,
    message_history: Sequence[ModelMessage] | None = None,
    timeout: float | None = None,
    usage: RunUsage | None = None,
) -> str:
    """运行并返回完整输出

    截断（`finish_reason == 'length'`）时把已生成内容作为历史回传并请求继续，拼接为完整文本。

    适用于无会话的一次性调用；带会话的流式场景由会话管理器自行实现续写。

    usage: 传入的`RunUsage`实例，原地累加所有请求的usage（供按Token计费）
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
            if usage is not None:
                usage.incr(result.usage)
            content = result.output
            if not isinstance(content, str) or not content.strip():
                raise RuntimeError("模型没有返回任何输出")
            parts.append(content)

            if (
                (m := result.all_messages()[-1])
                and isinstance(m, ModelResponse)
                and m.finish_reason != "length"
            ):
                return "".join(parts)

            # 截断：把已生成内容并入历史，从中断处续写
            history = result.all_messages()
            current = CONTINUE_PROMPT

    if timeout:
        async with asyncio.timeout(timeout):
            return await _run_all()
    return await _run_all()
