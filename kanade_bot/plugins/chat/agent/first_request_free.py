"""重置/压缩后的首次模型请求免费"""

from dataclasses import dataclass, field
from typing import Any

from nonebot import logger
from pydantic_ai import ModelRequestContext, ModelResponse, RunContext, RunUsage
from pydantic_ai.capabilities import AbstractCapability


@dataclass
class FirstRequestFreeCapability(AbstractCapability[Any]):
    """按会话兑现「重置/压缩后下一次模型请求免费」的名额"""

    _armed: set[str] = field(default_factory=set)
    """已登记免费名额、等待下一次模型请求兑现的会话集合"""

    def arm(self, session_id: str) -> None:
        """登记免费名额：该会话下一次模型请求免费（幂等）"""
        self._armed.add(session_id)

    def is_armed(self, session_id: str) -> bool:
        """该会话是否存在待兑现的免费名额"""
        return session_id in self._armed

    def clear(self) -> None:
        """清空全部待兑现名额（测试用，生产路径不应调用）"""
        self._armed.clear()

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        # 仅识别携带会话信息与豁免槽位的 deps（本 capability 只挂在聊天
        # agent 上，其它 deps 形态属测试/异构场景：不兑现、名额保留）
        info = getattr(ctx.deps, "session_info", None)
        session_id = info.session_id if info is not None else None
        waived: RunUsage | None = getattr(ctx.deps, "waived_usage", None)
        if session_id is None or waived is None or session_id not in self._armed:
            return response

        self._armed.discard(session_id)
        waived.incr(response.usage)
        usage = response.usage
        logger.info(
            f"会话{session_id}重置/压缩后首次请求免费已兑现："
            f"豁免 input={usage.input_tokens} output={usage.output_tokens}"
        )
        return response


first_request_free = FirstRequestFreeCapability()
"""模块级单例：manager 挂载到聊天 agent，压缩/重置路径经 `arm()` 登记"""
