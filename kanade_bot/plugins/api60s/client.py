from httpx import AsyncClient
from nonebot import get_plugin_config

from .config import Config

cfg = get_plugin_config(Config)

client = AsyncClient(base_url=cfg.api60s_base_url)
