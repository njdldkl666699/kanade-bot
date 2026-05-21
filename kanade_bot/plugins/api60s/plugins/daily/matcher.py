from nonebot import on_command

daily60s = on_command(
    "每天60秒读懂世界",
    priority=2,
    aliases={"60s", "60秒", "日报"},
    block=True,
)


ai_news = on_command(
    "AI资讯快报",
    aliases={"ai新闻", "AI资讯", "ai_news", "ai-news"},
    priority=2,
    block=True,
)


epic = on_command(
    "Epic",
    aliases={"epic", "epic游戏"},
    priority=2,
    block=True,
)


it_news = on_command(
    "实时IT资讯",
    aliases={"it新闻", "IT资讯", "it_news", "it-news"},
    priority=2,
    block=True,
)
