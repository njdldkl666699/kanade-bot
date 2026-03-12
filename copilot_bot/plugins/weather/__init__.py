from nonebot import get_plugin_config, on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot.rule import to_me

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="weather",
    description="",
    usage="",
    config=Config,
)

config = get_plugin_config(Config)

weather = on_command("天气", rule=to_me(), aliases={"weather", "查天气"}, priority=5, block=True)


@weather.handle()
async def handle_weather(args: Message = CommandArg()):
    # 提取参数纯文本作为地名，并判断是否有效
    if location := args.extract_plain_text():
        await weather.finish(f"今天{location}的天气是...")
    else:
        await weather.finish("请输入地名")
