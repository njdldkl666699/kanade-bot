"""主动回复子系统：防抖 → 退避 → 时段概率闸 → LLM 决策 → 回复执行"""

from .debounce import MessageDebouncer
from .executor import PROACTIVE_NOTIFICATION, build_proactive_notification, send_proactive_reply
from .gate import ReplyDecision, cfg, gate_system_prompt
from .gate_history import GateMeta, SessionGateHistory, build_meta_prompt
from .runtime import ProactiveRuntime, proactive_runtime, set_group_enabled
from .windows import describe_window, window_probability

__all__ = [
    "PROACTIVE_NOTIFICATION",
    "GateMeta",
    "MessageDebouncer",
    "ProactiveRuntime",
    "ReplyDecision",
    "SessionGateHistory",
    "build_meta_prompt",
    "build_proactive_notification",
    "cfg",
    "describe_window",
    "gate_system_prompt",
    "proactive_runtime",
    "send_proactive_reply",
    "set_group_enabled",
    "window_probability",
]
