"""主动回复子系统：防抖 → 退避 → 时段概率闸 → LLM 决策 → 回复执行"""

from .debounce import MessageDebouncer
from .executor import AUTO_REPLY_NOTIFICATION, build_auto_reply_notification, send_auto_reply
from .gate import ReplyDecision, cfg, gate_system_prompt
from .gate_history import GateMeta, SessionGateHistory, build_meta_prompt
from .runtime import AutoReplyRuntime, auto_reply_runtime, set_group_enabled
from .windows import describe_window, window_probability

__all__ = [
    "AUTO_REPLY_NOTIFICATION",
    "AutoReplyRuntime",
    "GateMeta",
    "MessageDebouncer",
    "ReplyDecision",
    "SessionGateHistory",
    "auto_reply_runtime",
    "build_auto_reply_notification",
    "build_meta_prompt",
    "cfg",
    "describe_window",
    "gate_system_prompt",
    "send_auto_reply",
    "set_group_enabled",
    "window_probability",
]
