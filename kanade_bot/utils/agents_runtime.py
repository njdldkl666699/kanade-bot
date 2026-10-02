"""openai-agents SDK 模型接入层。

将 `BaseAgentConfig`转换为 Agents SDK 的模型实例与 `ModelSettings`，
并按 `(base_url, api_key, model)` 缓存，避免每次运行重建连接池。
"""

import asyncio
import inspect
import os
from collections.abc import AsyncIterator
from contextvars import ContextVar
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    OpenAIChatCompletionsModel,
    Runner,
    TResponseInputItem,
    set_tracing_disabled,
)
from openai import AsyncOpenAI
from openai.types.shared import Reasoning

from kanade_bot.utils.schema import BaseAgentConfig, ProviderConfig

# 非OpenAI平台key，禁用tracing避免401噪音与额外开销
set_tracing_disabled(disabled=True)

type _ClientKey = tuple[str | None, str | None, tuple[tuple[str, str], ...] | None]
"""客户端缓存键：(base_url, api_key, headers键值对元组)"""

_client_cache: dict[_ClientKey, AsyncOpenAI] = {}
_model_cache: dict[tuple[_ClientKey, str], OpenAIChatCompletionsModel] = {}


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


def get_model(config: BaseAgentConfig) -> OpenAIChatCompletionsModel:
    """获取（并缓存）配置对应的Chat Completions模型实例"""
    if not config.model:
        raise ValueError("未配置模型ID（config: model）")

    provider = config.provider
    cache_key = (_client_key(provider), config.model)
    model = _model_cache.get(cache_key)
    if model is None:
        model = OpenAIChatCompletionsModel(
            model=config.model,
            openai_client=_get_client(provider),
        )
        _model_cache[cache_key] = model
    return model


def build_model_settings(config: BaseAgentConfig) -> ModelSettings:
    """将配置映射为 `ModelSettings`（reasoning_effort / max_output_tokens）"""
    reasoning = Reasoning(effort=config.reasoning_effort) if config.reasoning_effort else None
    return ModelSettings(
        reasoning=reasoning,
        max_tokens=config.max_output_tokens,
    )


# ===== 截断跟踪（max_output_tokens触发finish_reason=length的检测） =====

_finish_reason_holder: ContextVar[dict | None] = ContextVar("chatcmpl_finish_reason", default=None)
"""finish_reason共享容器（`{"finish_reason": "length" | "stop" | ...}`）

不能用裸ContextVar存值：`Runner.run`内部会创建子task，子task里的`set()`
对外层不可见（task运行在拷贝的context中）；但外层set的**可变容器**会被
子task继承引用，对其内容的修改对外层可见。
"""


def begin_length_tracking() -> dict:
    """开始一次截断跟踪：在当前asyncio任务上下文安装共享容器

    需在与后续`Runner.run`相同的任务上下文中调用；run结束后用
    `tracker.get("finish_reason")`读取（`"length"`表示输出被截断）。
    """
    tracker: dict = {}
    _finish_reason_holder.set(tracker)
    return tracker


class LengthTrackedChatCompletionsModel(OpenAIChatCompletionsModel):
    """截断跟踪模型：把finish_reason写入共享容器供应用层检测

    SDK的Chat Completions适配层在构造ModelResponse/流事件时丢弃了finish_reason，
    截断（有部分内容时）表现为静默返回半截输出；这里在原始响应的必经之路
    `_fetch_response`上挂钩子（非流式读choices，流式包装chunk流），
    使拼接续写成为可能。
    """

    async def _fetch_response(
        self,
        system_instructions: str | None,
        input: str | list,
        model_settings: ModelSettings,
        tools: list,
        output_schema,
        handoffs,
        span,
        tracing,
        stream: bool,
        prompt=None,
    ) -> Any:
        result: Any = await super()._fetch_response(
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            span,
            tracing,
            stream=stream,
            prompt=prompt,
        )
        if stream:
            # 流式：返回(response, stream)，包装stream逐chunk捕获finish_reason
            response, chunk_stream = result
            return response, self._tracked_stream(chunk_stream)
        choices = getattr(result, "choices", None)
        if choices:
            tracker = _finish_reason_holder.get()
            if tracker is not None:
                tracker["finish_reason"] = choices[0].finish_reason
        return result

    @staticmethod
    async def _tracked_stream(stream: AsyncIterator) -> AsyncIterator:
        """透传chunk流，捕获非空finish_reason；透传关闭到底层流"""
        try:
            async for chunk in stream:
                for choice in getattr(chunk, "choices", None) or ():
                    if choice.finish_reason:
                        tracker = _finish_reason_holder.get()
                        if tracker is not None:
                            tracker["finish_reason"] = choice.finish_reason
                yield chunk
        finally:
            # 父类对本包装调用aclose时，负责关闭底层流（连接归还）
            close = getattr(stream, "aclose", None) or getattr(stream, "close", None)
            if callable(close):
                close_result = close()
                if inspect.isawaitable(close_result):
                    await close_result


_tracked_model_cache: dict[tuple[_ClientKey, str], LengthTrackedChatCompletionsModel] = {}


def get_length_tracked_model(config: BaseAgentConfig) -> LengthTrackedChatCompletionsModel:
    """获取（并缓存）截断跟踪模型实例，供需要拼接续写的场景使用"""
    if not config.model:
        raise ValueError("未配置模型ID（config: model）")

    cache_key = (_client_key(config.provider), config.model)
    model = _tracked_model_cache.get(cache_key)
    if model is None:
        model = LengthTrackedChatCompletionsModel(
            model=config.model,
            openai_client=_get_client(config.provider),
        )
        _tracked_model_cache[cache_key] = model
    return model


# ===== 拼接续写（无session的一次性调用场景） =====

CONTINUE_PROMPT = "Please continue from where you left off."
"""截断后续写的提示语"""


async def run_with_continuation(
    agent: Agent,
    input_items: str | list[TResponseInputItem],
    *,
    max_turns: int = 1,
    timeout: float | None = None,
) -> str:
    """运行并返回完整输出：截断（finish_reason=length）时把已生成内容
    作为助手消息回传并请求继续，拼接为完整文本。

    适用于无session的一次性调用；带session的流式场景由会话管理器自行实现续写。
    """
    parts: list[str] = []
    current: str | list[TResponseInputItem] = input_items
    while True:
        tracker = begin_length_tracking()
        coro = Runner.run(agent, current, max_turns=max_turns)
        result = await (asyncio.wait_for(coro, timeout=timeout) if timeout else coro)
        content = result.final_output
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("模型没有返回任何输出")
        parts.append(content)

        if tracker.get("finish_reason") != "length":
            return "".join(parts)

        # 截断：把已生成内容回传，从中断处续写
        original: list = (
            [{"role": "user", "content": input_items}]
            if isinstance(input_items, str)
            else list(input_items)
        )
        current = [
            *original,
            {"role": "assistant", "content": "".join(parts)},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]
