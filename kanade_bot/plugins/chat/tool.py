import base64

from copilot import define_tool
from copilot.tools import ToolBinaryResult, ToolInvocation, ToolResult
from nonebot import logger
from pydantic import BaseModel, Field

from .client import file_client, tavily_client
from .config import configs
from .memory import MemoryType, WriteMode, read_memory_content, write_memory_content


class TavilySearchParams(BaseModel):
    query: str = Field(description="Search query.")


class ReadMemoryParams(BaseModel):
    memory_type: MemoryType = Field(
        description="Memory type to read: user for current user, group for current group chat, session for current session."
    )


class WriteMemoryParams(BaseModel):
    memory_type: MemoryType = Field(
        description="Memory type to write: user for current user, group for current group chat, session for current session."
    )
    content: str = Field(
        description="Markdown content to write. In replace mode this is the full file content; in append mode it is added to the end."
    )
    mode: WriteMode = Field(
        default="replace",
        description="Write mode: replace overwrites current memory, append adds to the end.",
    )


@define_tool(
    "web_search_tavily",
    description="""A web search tool that uses Tavily to search the web for relevant content. 
Ideal for gathering current information, news, and detailed web content analysis.""",
)
async def tavily_search(params: TavilySearchParams):
    response = await tavily_client.post(
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
    response = await tavily_client.post(
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
    description="""List the available meme dictionary where keys are meme names and values are descriptions.
To use a meme in a reply, reference it as {{meme_name}}, for example {{happy}};
it will be automatically replaced with the corresponding meme image when sent.""",
)
def list_memes():
    return configs.memes


@define_tool(
    "read_memory",
    description="""Read your memory.
Memory types:
- user: long-term memory for the current user, shared across groups and sessions.
- group: long-term memory for the current group chat only.
- session: short-term memory for the current group/private session, cleared on reset.
Use this to retrieve recorded preferences, facts, agreements, or temporary session notes, and any other content that needs to be remembered persistently.""",
)
def read_memory(params: ReadMemoryParams, invocation: ToolInvocation) -> str:
    logger.info("模型调用工具{}，读取{}记忆", read_memory.name, params.memory_type)
    return read_memory_content(params.memory_type, invocation.session_id)


@define_tool(
    "write_memory",
    description="""Write your memory.
Memory types:
- user: long-term memory for the current user, shared across groups and sessions; good for stable preferences and long-term facts.
- group: long-term memory for the current group chat only; good for group agreements, context, and shared preferences.
- session: short-term memory for the current group/private session, cleared on reset; good for temporary state.
Use this to record preferences, facts, agreements, or temporary session notes, and any other content that needs to be remembered persistently.""",
)
def write_memory(params: WriteMemoryParams, invocation: ToolInvocation) -> str:
    logger.info(
        "模型调用工具{}，写入{}记忆，模式：{}",
        write_memory.name,
        params.memory_type,
        params.mode,
    )
    write_memory_content(
        params.memory_type,
        invocation.session_id,
        params.content,
        params.mode,
    )
    return f"已写入 {params.memory_type} 记忆。"


@define_tool(
    "view_image",
    description="Image viewer tool. Provide one or more image URLs and it returns the image binary content.",
)
async def view_image(urls: list[str]) -> ToolResult:
    binary_results_for_llm: list[ToolBinaryResult] = []
    error_urls: list[str] = []

    for url in urls:
        response = await file_client.get(url)
        logger.info(
            "模型调用工具{}，查看图片：{}，返回了结果，状态码：{}",
            view_image.name,
            url,
            response.status_code,
        )
        if response.status_code == 200:
            image = ToolBinaryResult(
                data=base64.b64encode(response.content).decode("utf-8"),
                mime_type=response.headers.get("Content-Type", "application/octet-stream"),
                type="blob",
                description=url,
            )
            binary_results_for_llm.append(image)
        else:
            error_urls.append(url)

    error = None
    if error_urls:
        logger.warning(
            "模型调用工具{}，查看图片时以下URL返回了错误状态码：{}",
            view_image.name,
            error_urls,
        )
        error = f"以下URL解析失败：{error_urls}"

    return ToolResult(
        text_result_for_llm="图片查看结果",
        error=error,
        binary_results_for_llm=binary_results_for_llm,
    )
