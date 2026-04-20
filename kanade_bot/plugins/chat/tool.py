from copilot import define_tool
from nonebot import logger
from pydantic import BaseModel, Field

from .client import tavily_client as client
from .config import configs


class TavilySearchParams(BaseModel):
    query: str = Field(description="Search query.")


@define_tool(
    "web_search_tavily",
    description="""A web search tool that uses Tavily to search the web for relevant content. 
Ideal for gathering current information, news, and detailed web content analysis.""",
)
async def tavily_search(params: TavilySearchParams):
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


@define_tool(
    "web_page_extract_tavily",
    description="Extract the content of a web page using Tavily.",
)
async def tavily_extract(url: str):
    response = await client.post(
        "/extract",
        json={"url": [url]},
    )
    logger.info(
        "模型调用工具{}，提取页面：{}，返回了结果，状态码：{}",
        tavily_extract.name,
        url,
        response.status_code,
    )
    return response.json()


@define_tool(
    "list_memes",
    description="""列出当前可用的表情包字典，键为表情包名称，值为表情包描述。
需要在回复的消息中使用表情包时，只需使用{{表情包名称}}的格式引用它们，
例如{{开心}}，发送时将自动替换为相应的表情包图片。""",
)
def list_memes():
    return configs.memes
