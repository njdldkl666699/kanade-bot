from copilot import define_tool
from nonebot import logger
from pydantic import BaseModel, Field

from .client import tavily_client as client
from .config import configs


class TavilySearchParams(BaseModel):
    query: str = Field(description="要搜索的查询字符串")


@define_tool(
    "tavily_search",
    description="""一个网络搜索工具。
提供一个查询字符串来获取搜索结果。
必须等到它返回结果后才能再次调用它。
如果你需要关于同一查询的更多信息，
调用web_fetch工具从结果中的URL提取内容，
而不是再次调用它进行搜索。""",
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
    "list_memes",
    description="""列出当前可用的表情包字典，键为表情包名称，值为表情包描述。
需要在回复的消息中使用表情包时，只需使用{{表情包名称}}的格式引用它们，
例如{{开心}}，发送时将自动替换为相应的表情包图片。""",
)
def list_memes():
    return configs.memes
