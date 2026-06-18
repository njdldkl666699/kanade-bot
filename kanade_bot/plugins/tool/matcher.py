from nonebot import on_command, on_notice
from nonebot.adapters.onebot.v11 import GROUP
from nonebot.permission import SUPERUSER
from nonebot.rule import to_me

thunder_link_parse = on_command(
    "迅雷链接解析",
    aliases={"thunder_parse", "thunder_link_parse"},
    priority=2,
    block=True,
)


pjsk_skill_multiplier = on_command(
    "技能倍率",
    aliases={"倍率"},
    priority=2,
    block=True,
)


mc_status = on_command(
    "我的世界服务器状态",
    aliases={"mcstatus", "mcping", "mc_status", "mc_ping"},
    priority=2,
    block=True,
)


list_schedules = on_command(
    "定时任务列表",
    aliases={"schedule_list"},
    priority=2,
    block=True,
)


add_a_schedule = on_command(
    "添加定时任务",
    aliases={"schedule_add"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


remove_a_schedule = on_command(
    "移除定时任务",
    aliases={"schedule_remove"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)


send_emoji_like = on_command(
    "回应表情",
    aliases={"贴", "回复表情"},
    priority=2,
    permission=GROUP,
    block=True,
)


send_a_poke = on_command(
    "戳一戳",
    aliases={"poke", "戳"},
    priority=2,
    block=True,
)


receive_poke = on_notice(
    rule=to_me(),
    priority=100,
    block=True,
)


send_like = on_command(
    "点赞",
    aliases={"like", "赞", "赞我"},
    priority=2,
    block=True,
)


send_face = on_command(
    "发送表情",
    aliases={"send_face", "发表情"},
    priority=2,
    block=True,
)
