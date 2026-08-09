from nonebot import get_driver, on_command, on_notice, require
from nonebot.adapters.onebot.v11 import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    PrivateMessageEvent,
)
from nonebot.permission import SUPERUSER

from kanade_bot.utils.onebot11 import BotOfflineNoticeEvent

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

help_command = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)
register_matcher(help_command, "帮助")


def is_offline_notice_event(event: BotOfflineNoticeEvent):
    return True


offline_notice = on_notice(
    rule=is_offline_notice_event,
    priority=1,
    block=False,
)


def superuser_onebot_private_permission(event: PrivateMessageEvent) -> bool:
    """匹配OneBot私聊消息类型事件且发送者是超级用户"""
    return event.get_user_id() in get_driver().config.superusers


execute_command = on_command(
    "execute",
    aliases={"exec"},
    priority=2,
    permission=superuser_onebot_private_permission,
    block=True,
)
register_matcher(execute_command, "execute")


def is_group_increase_notice_event(event: GroupIncreaseNoticeEvent):
    return True


welcome = on_notice(
    rule=is_group_increase_notice_event,
    priority=1,
    block=False,
)


def is_group_decrease_notice_event(event: GroupDecreaseNoticeEvent):
    return True


leave_notice = on_notice(
    rule=is_group_decrease_notice_event,
    priority=1,
    block=False,
)

recall_message = on_command(
    "撤回消息",
    aliases={"recall", "撤回"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(recall_message, "撤回消息")
