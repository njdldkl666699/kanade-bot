"""主动回复 LLM 决策节点（reply gate）的模型构建与窗口固化"""

from nonebot import logger
from pydantic import BaseModel
from pydantic_ai import Agent

from kanade_bot.utils.pai_runtime import (
    build_model_settings,
    get_model,
    resolve_model_context_window,
)
from kanade_bot.utils.schema import BaseAgentConfig

from ...config import cfg
from ..prompt import PLACEHOLDER_PATTERN, _bot_name

GATE_SYSTEM_PROMPT = """你是{{bot_name}}的「插话决策器」，用于判断{{bot_name}}此刻是否应该主动插话。
下面是你在旁观的群聊记录（含你过去的判定）。
输出格式 JSON：{"reply": bool, "reason": "一句理由"}"""
"""内置默认插话决策提示词，`{{bot_name}}` 占位符渲染时替换为 Bot 昵称"""


class ReplyDecision(BaseModel):
    """决策节点结构化输出"""

    reply: bool
    """是否主动插话"""

    reason: str
    """一句理由"""


# ===== 模型解析（进程级缓存） =====

_gate_agent: Agent[None, ReplyDecision] | None = None
"""决策 Agent 进程级单例：系统提示与 output schema 恒定，保证前缀缓存命中"""

_context_window: int | None = None
_context_window_resolved = False


def effective_gate_config() -> BaseAgentConfig:
    """决策节点生效模型配置：`gate` 留空（未配 model）时回退主模型 `cfg.agent`"""
    gate = cfg.auto_reply.gate
    return gate if gate.model else cfg.agent


def _substitute_bot_name(template: str) -> str:
    """把模板中的 `{{ bot_name }}` 占位符替换为 Bot 昵称（`_bot_name()`）

    昵称来自 nonebot `nickname` 配置，进程内恒定，不影响前缀缓存。
    """
    return PLACEHOLDER_PATTERN.sub(
        lambda m: _bot_name() if m.group(1) == "bot_name" else m.group(0), template
    )


def gate_system_prompt() -> str:
    """插话决策系统提示词：`gate.system_prompt_file` 配置文件优先，回退内置默认

    `{{ bot_name }}` 占位符替换为 Bot 昵称；进程内首次构建 Agent 时读一次
    （提示词须逐字节稳定以保前缀缓存命中），后续修改文件需重启生效。
    """
    path = cfg.auto_reply.gate.system_prompt_file_path
    if path.is_file():
        if text := path.read_text(encoding="utf-8").strip():
            return _substitute_bot_name(text)
        logger.warning(f"决策系统提示词文件为空，使用内置默认：{path}")
    else:
        logger.warning(f"决策系统提示词文件不存在，使用内置默认：{path}")
    return _substitute_bot_name(GATE_SYSTEM_PROMPT)


def get_gate_agent() -> Agent[None, ReplyDecision]:
    """构建（或返回缓存的）决策 Agent"""
    global _gate_agent
    if _gate_agent is None:
        config = effective_gate_config()
        _gate_agent = Agent(
            name="kanade-bot-reply-gate",
            instructions=gate_system_prompt(),
            model=get_model(config),
            model_settings=build_model_settings(config),
            output_type=ReplyDecision,
        )
    return _gate_agent


def gate_context_window() -> int | None:
    """决策模型的上下文窗口（显式配置 > /models 元数据 > genai-prices），进程内固化"""
    global _context_window, _context_window_resolved
    if not _context_window_resolved:
        try:
            _context_window = resolve_model_context_window(effective_gate_config())
        except Exception as e:
            logger.warning(f"决策模型上下文窗口解析失败（截断阈值不可用）：{e}")
            _context_window = None
        _context_window_resolved = True
    return _context_window


def gate_limit_tokens() -> int | None:
    """决策上下文触发上限：窗口 × `summary_target_fraction`（None 回退 `trigger_fraction`）"""
    window = gate_context_window()
    if window is None:
        return None
    fraction = cfg.compaction.summary_target_fraction or cfg.compaction.trigger_fraction
    return max(1, int(window * fraction))
