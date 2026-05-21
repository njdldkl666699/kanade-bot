import random
from typing import Literal

from httpx import AsyncClient
from nonebot import get_plugin_config
from pydantic import BaseModel

from .config import Config

cfg = get_plugin_config(Config).fun

type LoliconSize = Literal["original", "regular", "small", "thumb", "mini"]


class LoliconRequest(BaseModel):
    """Lolicon API v2 /setu 请求体"""

    r18: Literal[0, 1, 2] = 0
    """0为非R18，1为R18，2为混合"""

    num: int = 1
    """一次返回的结果数量，范围为1到20"""

    uid: list[int] | None = None
    """返回指定uid作者的作品，最多20个"""

    keyword: str | None = None
    """返回从标题、作者、标签中按指定关键字模糊匹配的结果，大小写不敏感"""

    tag: list[str] | list[list[str]] | None = None
    """返回匹配指定标签的作品；POST 请求可直接发送二维数组"""

    size: list[LoliconSize] | LoliconSize = "original"
    """返回指定图片规格的地址"""

    proxy: str = "i.pixiv.re"
    """设置图片地址所使用的在线反代服务"""

    dateAfter: int | None = None
    """返回在这个时间及以后上传的作品；时间戳，单位为毫秒"""

    dateBefore: int | None = None
    """返回在这个时间及以前上传的作品；时间戳，单位为毫秒"""

    dsc: bool = False
    """禁用对某些缩写keyword和tag的自动转换"""

    excludeAI: bool = False
    """排除AI作品"""

    aspectRatio: str | None = None
    """图片长宽比筛选"""


class LoliconSetu(BaseModel):
    """Lolicon API v2 /setu 响应中的单条色图数据"""

    pid: int
    """作品 pid"""

    p: int
    """作品所在页"""

    uid: int
    """作者 uid"""

    title: str
    """作品标题"""

    author: str
    """作者名（入库时，并过滤掉 @ 及其后内容）"""

    r18: bool
    """是否 R18（在库中的分类，不等同于作品本身的 R18 标识）"""

    width: int
    """原图宽度 px"""

    height: int
    """原图高度 px"""

    tags: list[str]
    """作品标签，包含标签的中文翻译（有的话）"""

    ext: str
    """图片扩展名"""

    aiType: int
    """是否是 AI 作品，0 未知（旧画作或字段未更新），1 不是，2 是"""

    uploadDate: int
    """作品上传日期；时间戳，单位为毫秒"""

    urls: dict[LoliconSize, str]
    """包含了所有指定 size 的图片地址"""


class LoliconResponse(BaseModel):
    """Lolicon API v2 /setu 响应体"""

    error: str | None = None
    """错误信息"""

    data: list[LoliconSetu] = []
    """色图数组"""


LOLI_API_URL = "https://www.loliapi.com/bg/?type=url"

LOLICON_API_URL = "https://api.lolicon.app/setu/v2"

client = AsyncClient()


async def random_loli_waifu() -> str:
    url_resp = await client.get(LOLI_API_URL)
    return url_resp.text


async def query_lolicon_waifus(json_str: str = "{}") -> list[str]:
    request = LoliconRequest.model_validate_json(json_str)
    request.proxy = cfg.lolicon_proxy
    request.size = "regular"

    resp = await client.post(
        LOLICON_API_URL,
        json=request.model_dump(),
    )
    response = LoliconResponse.model_validate(resp.json())

    urls: list[str] = []
    for setu in response.data:
        urls.extend(setu.urls.values())
    return urls


async def get_random_waifu() -> str:
    i = random.randint(0, 1)
    if i == 0:
        return await random_loli_waifu()
    else:
        waifus = await query_lolicon_waifus()
        return waifus[0]
