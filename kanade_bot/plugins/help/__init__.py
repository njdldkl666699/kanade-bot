import time
import tomllib
from functools import lru_cache
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, on_command, require
from nonebot.adapters import Message
from nonebot.adapters.console import Bot as ConsoleBot
from nonebot.adapters.console import MessageSegment as ConsoleMessageSegment
from nonebot.adapters.onebot.v11 import Bot as OneBot
from nonebot.adapters.onebot.v11 import MessageSegment as OneBotMessageSegment
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata

from .config import Config

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import md_to_pic

__plugin_meta__ = PluginMetadata(
    name="help",
    description="帮助",
    usage="",
    config=Config,
)

cfg = get_plugin_config(Config)


@lru_cache(maxsize=1)
def get_project_version() -> str:
    """获取项目版本号"""
    pyproject_content = Path("pyproject.toml").read_text(encoding="utf-8")
    project_data = tomllib.loads(pyproject_content)
    return project_data["project"]["version"]


def get_help_md(doc_name: str) -> str | None:
    """确保帮助文档存在，如果文档不存在则返回None"""
    doc_path = Path(cfg.help_docs_dir_path) / f"{doc_name}.md"
    if not doc_path.is_file():
        logger.warning(f"帮助文档文件不存在，路径: {doc_path.absolute()}")
        return None

    # 读取文档内容并替换版本占位符
    return doc_path.read_text(encoding="utf-8").replace(
        "{{project_version}}", get_project_version()
    )


async def ensure_help_image(doc_name: str) -> bytes | None:
    """确保帮助文档图片存在且未过期，如果图片不存在或过期则重新生成并返回图片内容"""
    pic_name = f"{doc_name}-{get_project_version()}.png"
    pic_dir = Path(cfg.help_images_cache_dir_path)
    pic_dir.mkdir(parents=True, exist_ok=True)

    pic_path = pic_dir / pic_name
    if pic_path.is_file():
        # 图片存在且未过期，直接读取图片
        return pic_path.read_bytes()

    # 图片不存在或过期
    # 删除过期图片
    for file in pic_dir.glob(f"{doc_name}-*.png"):
        file.unlink()

    # 重新生成图片并保存
    help_md = get_help_md(doc_name)
    if not help_md:
        return None

    logger.info(f"生成 {doc_name} 帮助文档图片，路径: {pic_path.absolute()}")
    start = time.time()
    help_image = await md_to_pic(help_md)
    end = time.time()
    logger.info(f"生成 {doc_name} 帮助文档图片完成，耗时: {end - start:.2f}秒")

    pic_path.write_bytes(help_image)
    return help_image


help = on_command(
    "帮助",
    aliases={"help", "?", "帮助文档"},
    priority=2,
    block=True,
)

doc_names: set[str] = set()


@help.handle()
async def sakura_bot(arg_msg: Message = CommandArg()):
    sub_command = arg_msg.extract_plain_text().strip()
    if not sub_command:
        await help.send("Sakura Bot帮助文档：\n" + cfg.help_sakura_bot_link)


@help.handle()
async def _(bot: ConsoleBot, arg_msg: Message = CommandArg()):
    doc_name = arg_msg.extract_plain_text().strip()
    if doc_name not in doc_names:
        doc_name = "index"

    help_md = get_help_md(doc_name)
    if not help_md:
        help_md = "帮助文档不可用"

    await help.finish(ConsoleMessageSegment.markdown(help_md))


@help.handle()
async def _(bot: OneBot, arg_msg: Message = CommandArg()):
    doc_name = arg_msg.extract_plain_text().strip()
    if doc_name not in doc_names:
        doc_name = "index"

    help_image = await ensure_help_image(doc_name)
    if not help_image:
        await help.finish("帮助文档不可用")

    await help.finish(OneBotMessageSegment.image(help_image))


driver = get_driver()


@driver.on_startup
def on_startup():
    global doc_names

    doc_dir = Path(cfg.help_docs_dir_path)
    if not doc_dir.is_dir():
        logger.warning(f"帮助文档目录不存在，路径: {doc_dir.absolute()}")
        return

    doc_names = {file.stem for file in doc_dir.glob("*.md")}
    logger.info(f"加载帮助文档完成，文档列表: {doc_names}")
