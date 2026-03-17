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
    description=(
        "A web search tool. Provide a query string to get search results. "
        "This tool is relatively slow and usually takes about 10 seconds, "
        "so you need to wait until it returns results before calling it again. "
        "If you need more information about the same query, "
        "call the fetch tool to extract content from the URLs in the results, "
        "not call it again to search. "
    ),
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
