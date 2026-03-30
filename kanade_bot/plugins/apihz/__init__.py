from nonebot import on_command
from nonebot.adapters import Message
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from kanade_bot.plugins.util import parse_arg_message

from .config import Config
from .client import client
from .mc_server import McServerResponse, format_mc_server_data


__plugin_meta__ = PluginMetadata(
    name="apihz",
    description="接口盒子",
    usage="",
    config=Config,
)


morse_encode = on_command(
    "摩斯加密",
    aliases={"morse_encode", "摩斯编码"},
    priority=2,
    block=True,
)


@morse_encode.handle()
async def handle_morse_encoding(arg_msg: Message = CommandArg()):
    words = arg_msg.extract_plain_text()
    response = await client.get(
        "/zici/mosi.php",
        params={"type": 0, "words": words},
    )
    await morse_encode.finish(response.json()["words"])


morse_decode = on_command(
    "摩斯解密",
    aliases={"morse_decode", "摩斯解码"},
    priority=2,
    block=True,
)


@morse_decode.handle()
async def handle_morse_decoding(arg_msg: Message = CommandArg()):
    words = arg_msg.extract_plain_text()
    response = await client.get(
        "/zici/mosi.php",
        params={"type": 1, "words": words},
    )
    await morse_decode.finish(response.json()["words"])


mcserver = on_command(
    "Minecraft服务器信息查询",
    aliases={"mcserver", "mcping"},
    priority=2,
    block=True,
)


@mcserver.handle()
async def handle_mcserver(arg_msg: Message = CommandArg()):
    args = parse_arg_message(arg_msg, {"host": str, "port": int})
    host = args["host"]
    port = args.get("port", 25565)
    if not host:
        await mcserver.finish("参数错误，请使用：Minecraft服务器信息查询 <host> [port]")

    response = await client.get(
        "/fun/mcserver.php",
        params={
            "host": host,
            "port": port,
        },
    )

    result = McServerResponse.model_validate(response.json())
    data = result.data
    if result.code != 200 or not data.status:
        err = f"，错误信息：{data.error}" if data.error else ""
        await mcserver.finish(f"查询失败：{result.msg}{err}")

    await mcserver.finish(format_mc_server_data(data))
