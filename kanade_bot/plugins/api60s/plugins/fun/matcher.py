from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

hitokoto = on_command(
    "随机一言",
    aliases={"hitokoto", "一言"},
    priority=2,
    block=True,
)
register_matcher(hitokoto, "随机一言")

luck = on_command(
    "今日运势",
    aliases={"luck", "运势"},
    priority=2,
    block=True,
)
register_matcher(luck, "今日运势")

fabing = on_command(
    "随机发病",
    aliases={"fabing", "发病"},
    priority=2,
    block=True,
)
register_matcher(fabing, "随机发病")

answer = on_command(
    "随机答案之书",
    aliases={"答案之书", "随机答案", "bookofanswers"},
    priority=2,
    block=True,
)
register_matcher(answer, "随机答案之书")

kfc = on_command(
    "随机KFC文案",
    aliases={"KFC", "kfc", "肯德基", "疯狂星期四", "疯四", "v50", "v我50"},
    priority=2,
    block=True,
)
register_matcher(kfc, "随机KFC文案")

dad_joke = on_command(
    "随机冷笑话",
    aliases={"冷笑话", "dadjoke", "dad_joke"},
    priority=2,
    block=True,
)
register_matcher(dad_joke, "随机冷笑话")
