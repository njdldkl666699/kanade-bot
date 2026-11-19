"""重置/压缩后首次模型请求免费 capability 的单元测试

被测模块无相对导入（仅 pydantic_ai + nonebot.logger），按文件独立加载，
避开 `kanade_bot.plugins.chat` 包 `__init__` 连带启动 nonebot 插件运行时。
端到端行为（轮末计费扣除、重置登记）见 `test_pai_agent.py::FirstRequestFreeTest`。
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

from pydantic_ai import ModelResponse, RunUsage, TextPart
from pydantic_ai.usage import RequestUsage

REPO_ROOT = Path(__file__).parents[2]
MODULE_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/first_request_free.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("kchat_first_request_free", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


frf = _load_module()


def make_ctx(session_id: str = "g1", waived: RunUsage | None = None) -> SimpleNamespace:
    """构造最小 ctx 桩：capability 只读取 deps.session_info 与 deps.waived_usage"""
    deps = SimpleNamespace(
        session_info=SimpleNamespace(session_id=session_id),
        waived_usage=waived if waived is not None else RunUsage(),
    )
    return SimpleNamespace(deps=deps)


def resp(*, input_tokens: int = 100, output_tokens: int = 10, cache_read_tokens: int = 0):
    return ModelResponse(
        parts=[TextPart(content="hi")],
        usage=RequestUsage(
            input_tokens=input_tokens,
            cache_read_tokens=cache_read_tokens,
            output_tokens=output_tokens,
        ),
    )


class FirstRequestFreeCapabilityTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cap = frf.FirstRequestFreeCapability()

    async def _fire(self, ctx, response=None):
        return await self.cap.after_model_request(
            ctx, request_context=None, response=response or resp()
        )

    async def test_arm_then_first_request_claims(self):
        """登记后第一次模型请求兑现名额，usage 计入轮内豁免量"""
        ctx = make_ctx()
        self.cap.arm("g1")

        await self._fire(ctx, resp(input_tokens=1000, output_tokens=20, cache_read_tokens=800))

        waived = ctx.deps.waived_usage
        self.assertEqual(
            (waived.input_tokens, waived.output_tokens, waived.cache_read_tokens),
            (1000, 20, 800),
        )
        self.assertFalse(self.cap.is_armed("g1"), "名额应已兑现")

    async def test_second_request_not_waived(self):
        """同一轮的后续请求不再豁免（只免第一条）"""
        ctx = make_ctx()
        self.cap.arm("g1")
        await self._fire(ctx, resp(input_tokens=1000, output_tokens=20))
        await self._fire(ctx, resp(input_tokens=500, output_tokens=5))

        waived = ctx.deps.waived_usage
        self.assertEqual((waived.input_tokens, waived.output_tokens), (1000, 20))

    async def test_not_armed_is_noop(self):
        """未登记名额时请求正常通过，不产生豁免"""
        ctx = make_ctx()
        response = resp(input_tokens=1000)
        result = await self._fire(ctx, response)

        self.assertIs(result, response)
        self.assertEqual(ctx.deps.waived_usage.input_tokens, 0)

    async def test_rearm_grants_new_freebie(self):
        """再次压缩/重置可重新登记：名额按事件逐次发放"""
        ctx = make_ctx()
        self.cap.arm("g1")
        await self._fire(ctx, resp(input_tokens=1000))
        self.assertFalse(self.cap.is_armed("g1"))

        self.cap.arm("g1")
        ctx2 = make_ctx()
        await self._fire(ctx2, resp(input_tokens=300))
        self.assertEqual(ctx2.deps.waived_usage.input_tokens, 300)

    async def test_arm_is_idempotent(self):
        """多次登记幂等：仍只免一次"""
        ctx = make_ctx()
        self.cap.arm("g1")
        self.cap.arm("g1")
        await self._fire(ctx)
        await self._fire(ctx, resp(input_tokens=700))
        self.assertEqual(ctx.deps.waived_usage.input_tokens, 100)

    async def test_sessions_are_independent(self):
        """名额按会话隔离：其它会话的请求不消耗本会话名额"""
        self.cap.arm("g1")
        ctx_other = make_ctx(session_id="g2")
        await self._fire(ctx_other, resp(input_tokens=999))

        self.assertTrue(self.cap.is_armed("g1"))
        self.assertEqual(ctx_other.deps.waived_usage.input_tokens, 0)

    async def test_foreign_deps_keeps_freebie(self):
        """deps 无会话信息/豁免槽位（非聊天 agent 场景）：不兑现、名额保留"""
        self.cap.arm("g1")
        await self._fire(SimpleNamespace(deps=SimpleNamespace()))

        self.assertTrue(self.cap.is_armed("g1"))

    def test_clear_empties_all(self):
        self.cap.arm("g1")
        self.cap.clear()
        self.assertFalse(self.cap.is_armed("g1"))


if __name__ == "__main__":
    unittest.main()
