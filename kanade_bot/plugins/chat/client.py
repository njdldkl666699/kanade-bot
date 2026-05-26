from httpx import AsyncClient

from .config import cfg

file_client = AsyncClient()


tavily_client = AsyncClient(
    base_url="https://api.tavily.com",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.tavily_api_key}",
    },
)
