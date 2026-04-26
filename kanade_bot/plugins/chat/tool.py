from copilot import define_tool
from copilot.tools import ToolInvocation
from nonebot import logger
from pydantic import BaseModel, Field

from .client import tavily_client as client
from .config import configs
from .memory import MemoryType, WriteMode, read_memory_content, write_memory_content


class TavilySearchParams(BaseModel):
    query: str = Field(description="Search query.")


class ReadMemoryParams(BaseModel):
    memory_type: MemoryType = Field(
        description="要读取的记忆类型：user 为当前用户，group 为当前群聊，session 为当前会话。"
    )


class WriteMemoryParams(BaseModel):
    memory_type: MemoryType = Field(
        description="要写入的记忆类型：user 为当前用户，group 为当前群聊，session 为当前会话。"
    )
    content: str = Field(
        description="要写入的 Markdown 内容。replace 模式下这是完整文件内容；append 模式下会追加到文件末尾。"
    )
    mode: WriteMode = Field(
        default="replace",
        description="写入模式：replace 覆盖当前记忆，append 追加到当前记忆末尾。",
    )


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


@define_tool(
    "read_memory",
    description="""读取 Agent 的 Markdown 记忆。
记忆分为三类：
- user：当前用户的长期记忆，跨群聊、跨会话保留。
- group：当前群聊的长期记忆，只和当前群聊相关。
- session：当前群聊/私聊会话的短期记忆，重置会话时会被清空。
当需要了解已记录的偏好、事实、约定或当前会话暂存信息时使用。""",
)
def read_memory(params: ReadMemoryParams, invocation: ToolInvocation) -> str:
    logger.info("模型调用工具{}，读取{}记忆", read_memory.name, params.memory_type)
    return read_memory_content(params.memory_type, invocation.session_id)


@define_tool(
    "write_memory",
    description="""写入 Agent 的 Markdown 记忆。
记忆分为三类：
- user：当前用户的长期记忆，跨群聊、跨会话保留，适合记录用户偏好、长期事实和稳定约定。
- group：当前群聊的长期记忆，只和当前群聊相关，适合记录群内约定、群聊背景和共同偏好。
- session：当前群聊/私聊会话的短期记忆，重置会话时会被清空，适合记录当前话题的临时状态。
只记录用户明确表达或对后续对话明显有帮助的信息，内容使用 Markdown。""",
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
