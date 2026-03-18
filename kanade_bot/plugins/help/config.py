from pathlib import Path
import tomllib

from nonebot import get_driver, logger
from pydantic import BaseModel


class Config(BaseModel):
    help_link: str


PROJECT_VERSION: str = ""

driver = get_driver()


@driver.on_startup
async def on_startup():
    global PROJECT_VERSION
    project_data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    PROJECT_VERSION = project_data["project"]["version"]
    logger.info(f"Kanade Bot version: {PROJECT_VERSION}")
