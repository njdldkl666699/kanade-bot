import base64
from pathlib import Path

from mcstatus import JavaServer
from nonebot import get_plugin_config, logger, on_command
from nonebot.adapters import Event, Message
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotMessageEvent
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from kanade_bot.plugins.util import parse_arg_message

from .config import Config
from .mcstatus import render_mc_status

__plugin_meta__ = PluginMetadata(
    name="tool",
    description="",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)

thunder_link_parse = on_command(
    "迅雷链接解析",
    aliases={"thunder_parse", "thunder_link_parse"},
    priority=2,
    block=True,
)


@thunder_link_parse.handle()
async def _(arg_msg: Message = CommandArg()):
    thunder_link = arg_msg.extract_plain_text().strip()
    if not thunder_link.startswith("thunder://"):
        await thunder_link_parse.finish("请输入有效的迅雷链接")

    try:
        decoded_bytes = base64.b64decode(thunder_link[10:])
        decoded_str = decoded_bytes.decode("utf-8")
        if decoded_str.startswith("AA") and decoded_str.endswith("ZZ"):
            decoded_str = decoded_str[2:-2]
        await thunder_link_parse.finish(decoded_str)
    except Exception as e:
        await thunder_link_parse.finish(f"解析失败: {e}")


pjsk_skill_multiplier = on_command(
    "技能倍率",
    aliases={"倍率"},
    priority=2,
    block=True,
)


@pjsk_skill_multiplier.handle()
async def _(arg_msg: Message = CommandArg()):
    args = arg_msg.extract_plain_text().strip().split()
    multipliers = [int(arg) for arg in args if arg.isdigit()]
    if len(multipliers) != 5:
        await pjsk_skill_multiplier.finish("请输入5个技能倍率，格式如：/倍率 100 100 100 100 100")

    captain = multipliers[0]
    members = sum(multipliers[1:]) / 5
    total_multiplier = captain + members
    await pjsk_skill_multiplier.finish(
        "您的卡组技能效果如下\n"
        f"车头: {captain}%\n"
        f"内部: {members}%\n"
        f"倍率: {total_multiplier / 100 + 1}\n"
        f"技能实际值为: {total_multiplier}%"
    )


mc_status = on_command(
    "我的世界服务器状态",
    aliases={"mcstatus", "mcping", "mc_status", "mc_ping"},
    priority=2,
    block=True,
)


@mc_status.handle()
async def _(event: Event, arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"host": str, "port": int, "theme": str})
    host: str | None = args["host"]
    if host is None:
        await mc_status.finish("请提供服务器地址")
    port: int | None = args["port"]
    theme: str | None = args["theme"]
    if theme not in ("light", "dark"):
        theme = "light"

    try:
        server = JavaServer(host, port)
        status = await server.async_status()
    except Exception as e:
        logger.warning(f"查询服务器状态失败: {e}")
        await mc_status.finish("服务器查询失败")

    image = render_mc_status(status, host, port, theme)
    if isinstance(event, OneBotMessageEvent):
        # 发送图片消息
        await mc_status.finish(MessageSegment.image(image))

    # 其他平台保存图片文件
    image_path = Path("mc_status.png")
    image_path.write_bytes(image)
    await mc_status.finish("服务器状态已保存到 mc_status.png")
