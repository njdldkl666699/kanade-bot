"""上下文预算：token/请求体字节估算与请求输出预算 capability"""

import json
from collections.abc import Sequence
from copy import copy
from dataclasses import dataclass
from math import ceil
from typing import Any

from pydantic_ai import (
    BinaryContent,
    CompactionPart,
    ModelMessage,
    ModelRequest,
    ModelRequestContext,
    ModelResponse,
    ModelSettings,
    RunContext,
    SystemPromptPart,
    TextContent,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
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


def _json_text_bytes(text: str) -> int:
    """字符串在 JSON body 中的 UTF-8 字节数（含引号/转义）"""

    return len(json.dumps(text, ensure_ascii=False).encode("utf-8"))


def _binary_b64_bytes(data: bytes) -> int:
    """base64 编码后的字节数（含 padding）"""

    return 4 * ((len(data) + 2) // 3)


def _binary_content_bytes(content: BinaryContent) -> int:
    """BinaryContent 在 OpenAI data URL 中的体积估算"""

    # data:<mime>;base64,<data> + JSON 字段/引号开销
    return _binary_b64_bytes(content.data) + len(content.media_type) + 32


def _content_bytes(content: Any) -> int:
    """估算一段 UserContent/工具返回内容的请求体字节数"""

    if isinstance(content, str):
        return _json_text_bytes(content)
    if isinstance(content, TextContent):
        return _json_text_bytes(content.content)
    if isinstance(content, BinaryContent):
        return _binary_content_bytes(content)
    if isinstance(content, (list, tuple)):
        return sum(_content_bytes(item) for item in content)
    return _json_text_bytes(str(content))


def _part_bytes(part: Any) -> int:
    """单个消息 part 的请求体字节数估算（含 JSON 结构开销）"""

    if isinstance(part, UserPromptPart):
        return _content_bytes(part.content) + 48
    if isinstance(part, SystemPromptPart):
        return _content_bytes(part.content) + 48
    if isinstance(part, TextPart):
        return _content_bytes(part.content) + 48
    if isinstance(part, ThinkingPart):
        return _content_bytes(part.content or "") + 48
    if isinstance(part, ToolCallPart):
        if isinstance(part.args, str):
            args = part.args
        else:
            try:
                args = json.dumps(part.args, ensure_ascii=False)
            except (TypeError, ValueError):
                args = str(part.args)
        return _json_text_bytes(part.tool_name) + _json_text_bytes(args) + 64
    if isinstance(part, ToolReturnPart):
        return _content_bytes(part.content) + _json_text_bytes(part.tool_name) + 64
    if isinstance(part, CompactionPart):
        return _content_bytes(part.content) + 48

    content = getattr(part, "content", None)
    return _content_bytes(content) + 64 if content is not None else 64


def estimate_user_content_bytes(content: str | Sequence[UserContent]) -> int:
    """估算一轮尚未入库的用户内容的请求体字节数（不含外层消息结构）"""

    return _content_bytes(content)


def estimate_message_list_bytes(messages: Sequence[ModelMessage]) -> int:
    """估算消息列表在 OpenAI 兼容请求体中的字节数

    - 文本按 `json.dumps(ensure_ascii=False)` 的 UTF-8 字节计
    - 图片按 base64 体积 + data URL/mime 开销计
    - instructions 只计最近一次非空值（与真实请求一致）
    - 每个 part/message 附加少量 JSON 结构开销

    工具 schema 不在估算内；配置预算时应预留余量。
    """

    total = 0
    last_instructions = ""
    for message in messages:
        if isinstance(message, ModelRequest) and message.instructions:
            last_instructions = message.instructions
        total += sum(_part_bytes(part) for part in getattr(message, "parts", ())) + 96
    return total + _json_text_bytes(last_instructions)


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
    message_list_byte_budget: int | None = None
    """消息列表请求体字节预算；None 表示不检查"""

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
        if self.message_list_byte_budget is not None:
            estimated_bytes = estimate_message_list_bytes(request_context.messages)
            if estimated_bytes > self.message_list_byte_budget:
                raise ContextBudgetError(
                    f"消息列表请求体超出字节预算：估算 {estimated_bytes} 字节，"
                    f"预算 {self.message_list_byte_budget} 字节"
                )

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
