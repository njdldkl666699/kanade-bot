from copilot import define_tool
from httpx import AsyncClient
from nonebot import get_plugin_config, logger
from pydantic import BaseModel, Field

from .config import Config

cfg = get_plugin_config(Config)


client = AsyncClient(
    base_url="https://api.tavily.com",
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg.chat_tavily_api_key}",
    },
)


class TavilySearchParams(BaseModel):
    query: str = Field(description="string query to search")


@define_tool(
    "tavily_search",
    description="一个网络搜索工具。提供一个查询字符串来获取搜索结果。"
    "这个工具相对较慢，通常需要大约10秒钟，所以你需要等到它返回结果后才能再次调用它。"
    "如果你需要关于同一查询的更多信息，调用web_fetch工具从结果中的URL提取内容，"
    "而不是再次调用它进行搜索。",
)
async def tavily_search(params: TavilySearchParams):
    """Run a Tavily web search."""
    response = await client.post(
        "/search",
        json={
            "query": params.query,
            "search_depth": "basic",
            "max_results": 10,
            "country": "china",
            "exclude_domains": ["cndn.net"],
        },
    )
    logger.info(
        "模型调用工具{}，查询内容：{}，返回了结果，状态码：{}",
        tavily_search.name,
        params.query,
        response.status_code,
    )
    return response.json()
