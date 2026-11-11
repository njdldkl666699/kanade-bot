"""主动回复管理命令"""

from nonebot.adapters import Event

from kanade_bot.utils.common import get_platform_type
from kanade_bot.utils.session import extract_session_info_sync

from ..agent.proactive import proactive_runtime, set_group_enabled
from ..config import cfg
from ..matcher import decision_clear, decision_stats, proactive_disable, proactive_enable
from ..util import group_id_of


@proactive_enable.handle()
async def handle_proactive_enable(event: Event):
    """在当前群启用主动回复（SUPERUSER）"""
    if (group_id := group_id_of(event)) is None:
        await proactive_enable.finish("请在群聊中使用本命令")
    set_group_enabled(group_id, get_platform_type(event), True)
    message = f"已启用群 {group_id} 的主动回复"
    if not cfg.proactive.enabled:
        message += "\n注意：全局开关 chat.proactive.enabled 未开启，请先在配置文件中启用"
    await proactive_enable.finish(message)


@proactive_disable.handle()
async def handle_proactive_disable(event: Event):
    """在当前群禁用主动回复（SUPERUSER）"""
    if (group_id := group_id_of(event)) is None:
        await proactive_disable.finish("请在群聊中使用本命令")
    set_group_enabled(group_id, get_platform_type(event), False)
    await proactive_disable.finish(f"已禁用群 {group_id} 的主动回复")


@decision_stats.handle()
async def handle_decision_stats(event: Event):
    """输出当前会话的决策器统计"""
    session_info = extract_session_info_sync(event)
    group_label = session_info.group_id or session_info.session_id
    text = await proactive_runtime.format_stats(session_info.session_id, group_label)
    if text is None:
        await decision_stats.finish("当前会话未进入主动回复流水线（未启用或启动后尚无消息观察）")
    await decision_stats.finish(text)


@decision_clear.handle()
async def handle_decision_clear(event: Event):
    """清空当前会话的决策器上下文（SUPERUSER）"""
    session_id = extract_session_info_sync(event).session_id
    deleted = await proactive_runtime.clear_history(session_id)
    if deleted is None:
        await decision_clear.finish("当前会话未进入主动回复流水线（未启用或启动后尚无消息观察）")
    await decision_clear.finish(
        f"已清空决策器上下文（删除 {deleted} 条历史记录）\n"
        "决策器将从零开始重新旁观；统计计数与防抖/退避状态保留，主会话不受影响"
    )
