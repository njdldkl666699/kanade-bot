import time
import tomllib
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, on_command, require
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageSegment as ConsoleMessageSegment
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_htmlkit")

from nonebot_plugin_htmlkit import md_to_pic

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


help = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)

help_md: str | None = None
help_image: bytes | None = None

driver = get_driver()


@driver.on_startup
async def on_startup():
    doc_path = Path(cfg.help_doc_path)
    if not doc_path.is_file():
        logger.warning(f"帮助文档文件不存在，路径: {doc_path.absolute()}")
        return

    pyproject_content = Path("pyproject.toml").read_text(encoding="utf-8")
    project_data = tomllib.loads(pyproject_content)
    project_version: str = project_data["project"]["version"]

    help_md = doc_path.read_text(encoding="utf-8")
    # 替换{{project_version}}占位符
    help_md = help_md.replace("{{project_version}}", project_version)

    pic_name = f"{doc_path.stem}-{project_version}.png"
    pic_dir = Path(cfg.help_pic_cache_dir)
    pic_dir.mkdir(parents=True, exist_ok=True)

    pic_path = pic_dir / pic_name
    if pic_path.is_file():
        # 图片存在且未过期，直接读取图片
        help_image = pic_path.read_bytes()
        return

    # 图片不存在或过期
    # 删除过期图片
    for file in pic_dir.glob(f"{doc_path.stem}-*.png"):
        file.unlink()

    # 重新生成图片并保存
    logger.info(f"生成帮助文档图片，路径: {pic_path.absolute()}")
    start = time.time()
    help_image = await md_to_pic(help_md)
    end = time.time()
    logger.info(f"生成帮助文档图片完成，耗时: {end - start:.2f}秒")
    pic_path.write_bytes(help_image)


@help.handle()
async def handle_help(bot: Bot):
    if isinstance(bot, OneBot):
        # OneBot发送图片
        if not help_image:
            await help.finish("帮助文档不可用")
        await help.finish(OneBotMessageSegment.image(help_image))

    if not help_md:
        await help.finish("帮助文档不可用")

    if isinstance(bot, ConsoleBot):
        # Console发送Markdown文本
        await help.finish(ConsoleMessageSegment.markdown(help_md))

    await help.finish(help_md)
