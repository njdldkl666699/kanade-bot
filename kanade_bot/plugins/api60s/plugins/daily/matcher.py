from nonebot import on_command, require

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

daily60s = on_command(
    "每天60秒读懂世界",
    priority=2,
    aliases={"60s", "60秒", "日报"},
    block=True,
)
register_matcher(daily60s, "每天60秒读懂世界")

ai_news = on_command(
    "AI资讯快报",
    aliases={"ai新闻", "AI资讯", "ai_news", "ai-news"},
    priority=2,
    block=True,
)
register_matcher(ai_news, "AI资讯快报")

epic = on_command(
    "Epic",
    aliases={"epic", "epic游戏"},
    priority=2,
    block=True,
)
register_matcher(epic, "Epic游戏")

it_news = on_command(
    "实时IT资讯",
    aliases={"it新闻", "IT资讯", "it_news", "it-news"},
    priority=2,
    block=True,
)
register_matcher(it_news, "实时IT资讯")
