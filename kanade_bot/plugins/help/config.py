from pathlib import Path
import tomllib

from nonebot import get_driver
from pydantic import BaseModel


class Config(BaseModel):
    help_link: str


PROJECT_VERSION: str = ""

driver = get_driver()


@driver.on_startup
async def on_startup():
    global version
    project_data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project_version = project_data["project"]["version"]
    print(f"Kanade Bot version: {project_version}")
