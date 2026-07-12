from nonebot import on_command, on_notice, require
from nonebot.adapters.onebot.v11 import GROUP
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

thunder_link_parse = on_command(
    "迅雷链接解析",
    aliases={"thunder_parse", "thunder_link_parse"},
    priority=2,
    block=True,
)
register_matcher(thunder_link_parse, "迅雷链接解析")

pjsk_skill_multiplier = on_command(
    "技能倍率",
    aliases={"倍率"},
    priority=2,
    block=True,
)
register_matcher(pjsk_skill_multiplier, "技能倍率")

mc_status = on_command(
    "我的世界服务器状态",
    aliases={"mcstatus", "mcping", "mc_status", "mc_ping"},
    priority=2,
    block=True,
)
register_matcher(mc_status, "我的世界服务器状态")

mc_skin = on_command(
    "我的世界皮肤",
    aliases={"mcskin", "mc_skin", "玩家皮肤"},
    priority=2,
    block=True,
)
register_matcher(mc_skin, "我的世界皮肤")

list_schedules = on_command(
    "定时任务列表",
    aliases={"schedule_list"},
    priority=2,
    block=True,
)
register_matcher(list_schedules, "定时任务列表")

add_a_schedule = on_command(
    "添加定时任务",
    aliases={"schedule_add"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(add_a_schedule, "添加定时任务")

remove_a_schedule = on_command(
    "移除定时任务",
    aliases={"schedule_remove"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_a_schedule, "移除定时任务")

set_emoji_like = on_command(
    "回应表情",
    aliases={"贴", "回复表情"},
    priority=2,
    permission=GROUP,
    block=True,
)
register_matcher(set_emoji_like, "回应表情")

set_this_emoji_like = on_command(
    "回应这个表情",
    aliases={"贴这个", "回复这个表情"},
    priority=2,
    permission=GROUP,
    block=True,
)
register_matcher(set_this_emoji_like, "回应这个表情")

send_a_poke = on_command(
    "戳一戳",
    aliases={"poke", "戳"},
    priority=2,
    block=True,
)
register_matcher(send_a_poke, "戳一戳")

receive_poke = on_notice(
    rule=to_me(),
    priority=100,
    block=True,
)
register_matcher(receive_poke, "收到戳一戳")

send_like = on_command(
    "点赞",
    aliases={"like", "赞", "赞我"},
    priority=2,
    block=True,
)
register_matcher(send_like, "点赞")

send_face = on_command(
    "发送表情",
    aliases={"send_face", "发表情"},
    priority=2,
    block=True,
)
register_matcher(send_face, "发送表情")
