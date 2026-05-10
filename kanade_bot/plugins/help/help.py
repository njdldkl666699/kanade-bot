import time
import tomllib
from functools import lru_cache
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger, require

from .config import Config

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import md_to_pic

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


DOC_NAMES: set[str] = set()
"""帮助文档名称列表"""

driver = get_driver()


@driver.on_startup
def on_startup():
    global DOC_NAMES

    doc_dir = Path(cfg.help_docs_dir_path)
    if not doc_dir.is_dir():
        logger.warning(f"帮助文档目录不存在，路径: {doc_dir.absolute()}")
        return

    DOC_NAMES = {file.stem for file in doc_dir.glob("*.md")}
    logger.info(f"加载帮助文档完成，文档列表: {DOC_NAMES}")
