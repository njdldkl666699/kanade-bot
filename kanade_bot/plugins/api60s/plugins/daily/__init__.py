from nonebot import on_command
from nonebot.plugin import PluginMetadata

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="daily",
    description="60s API 🕘 周期资讯",
    usage="",
    config=Config,
)


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

__all__ = ["daily60s", "ai_news", "epic", "it_news"]
