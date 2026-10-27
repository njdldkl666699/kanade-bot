"""上下文预算：token 估算与请求输出预算 capability"""

from copy import copy
from dataclasses import dataclass
from math import ceil
from typing import Any

from pydantic_ai import (
    BinaryContent,
    ModelMessage,
    ModelRequestContext,
    ModelResponse,
    ModelSettings,
    RunContext,
    UserPromptPart,
)
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai_harness.compaction import estimate_context_tokens

MIN_OUTPUT_TOKENS = 4096
"""保留给正常回答的最小输出预算"""

MIN_SAFETY_MARGIN_TOKENS = 8192
"""输入估算之外的最小安全余量"""

IMAGE_TOKEN_ESTIMATE = 1024
"""DeepSeek视觉模型单张图片的保守token上限"""


class ContextBudgetError(RuntimeError):
    """请求在本地预算检查阶段已无法安全发送"""


def context_safety_margin(context_window: int) -> int:
    """为估算误差和工具 schema 预留的安全余量"""

    return max(MIN_SAFETY_MARGIN_TOKENS, ceil(context_window * 0.05))


def _image_content_count(messages: list[ModelMessage], start: int = 0) -> int:
    """统计 usage anchor 之后的图片数量"""
    total = 0
    for message in messages[start:]:
        for part in getattr(message, "parts", ()):
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content if not isinstance(part.content, str) else [part.content]
            total += sum(1 for item in content if isinstance(item, BinaryContent) and item.is_image)
    return total


def estimate_request_tokens(
    messages: list[ModelMessage],
    request_parameters: ModelRequestParameters | None = None,
    *,
    image_tokens_per_image: int = IMAGE_TOKEN_ESTIMATE,
) -> int:
    """估算即将发送的输入 tokens（harness 通用估算 + anchor 后图片按张补计）"""

    estimated = estimate_context_tokens(messages, model_request_parameters=request_parameters)
    anchor = -1
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, ModelResponse) and message.usage.input_tokens > 0:
            anchor = index
            break
    return estimated + _image_content_count(messages, anchor + 1) * image_tokens_per_image


@dataclass
class RequestBudgetCapability(AbstractCapability[Any]):
    """在压缩后的请求上重新计算输出预算，并阻止必然越界的请求。"""

    context_window: int | None
    output_upper_tokens: int | None
    image_tokens_per_image: int = IMAGE_TOKEN_ESTIMATE

    def get_model_settings(self):
        def resolve(ctx: RunContext[Any]) -> ModelSettings:
            settings = copy(ctx.model_settings) or ModelSettings()
            configured = self.output_upper_tokens or settings.get("max_tokens")
            if self.context_window is None:
                return {"max_tokens": configured} if configured else {}
            estimated = estimate_request_tokens(
                list(ctx.messages), image_tokens_per_image=self.image_tokens_per_image
            )
            available = self.context_window - estimated - context_safety_margin(self.context_window)
            requested = configured if isinstance(configured, int) else available
            return {"max_tokens": max(0, min(requested, available))}

        return resolve

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        window = self.context_window or request_context.model.context_window
        if window is None:
            return request_context

        estimated = estimate_request_tokens(
            request_context.messages,
            request_context.model_request_parameters,
            image_tokens_per_image=self.image_tokens_per_image,
        )
        available = window - estimated - context_safety_margin(window)
        if available < MIN_OUTPUT_TOKENS:
            raise ContextBudgetError(
                f"上下文预算不足：估算输入 {estimated} tokens，窗口 {window}，"
                f"安全余量 {context_safety_margin(window)}，无法保留至少 {MIN_OUTPUT_TOKENS} tokens 输出"
            )

        # 拷贝后修改
        settings = ModelSettings(request_context.model_settings or {})
        configured = self.output_upper_tokens or settings.get("max_tokens")
        effective = min(configured or available, available)
        if effective < MIN_OUTPUT_TOKENS:
            raise ContextBudgetError(
                f"上下文预算不足：可用输出 {effective} tokens，低于最小预算 {MIN_OUTPUT_TOKENS}"
            )
        settings["max_tokens"] = effective
        request_context.model_settings = settings
        return request_context
