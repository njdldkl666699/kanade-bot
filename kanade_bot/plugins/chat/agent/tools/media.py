"""图片类工具：查看图片、以图搜图、发送图片、HTML 渲染"""

import base64
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

import magic
from httpx import HTTPError
from nonebot import get_bot, logger, require
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from PicImageSearch import BaiDu
from pydantic_ai import BinaryContent, RunContext

from kanade_bot.utils.common import HTTPX_CLIENT
from kanade_bot.utils.onebot11 import get_image_path

from ...config import cfg
from ..deps import ChatDeps
from ..vision import get_image_caption
from .common import get_sandbox
from .messaging import send_to_session

require("nonebot_plugin_htmlrender")
from nonebot_plugin_htmlrender import get_default_application


async def _load_qq_image(ctx: RunContext[ChatDeps], file: str) -> bytes | None:
    """通过OneBot get_image接口拉取QQ消息中的图片内容，失败返回None"""
    try:
        bot = get_bot(ctx.deps.bot_id)
    except (KeyError, ValueError):
        logger.warning("无法获取Bot实例，bot_id: {}", ctx.deps.bot_id)
        return None
    if not isinstance(bot, Bot):
        return None
    try:
        # 复用get_image_path：兼容实现端返回本地路径或网络地址两种情况
        path = await get_image_path(bot, MessageSegment("image", {"file": file}))
    except Exception as e:
        logger.warning("获取QQ图片 {} 失败: {}", file, e)
        return None
    if not path.is_file():
        return None
    return path.read_bytes()


async def view_image(ctx: RunContext[ChatDeps], image: str) -> str | list[str | BinaryContent]:
    """查看一张图片，返回图片内容供视觉理解。

    Args:
        image: 图片来源，支持三种形式：
            - QQ消息中的图片文件名：消息里 `[图片：xxx]` 方括号内的 xxx，
              若启用沙箱，还会自动保存到沙箱工作区 images/；
            - http/https 网络地址；
            - 沙箱工作区内的相对路径（需启用沙箱）。
    """
    logger.info("查看图片工具被调用，来源: {}", image)
    scheme = urlparse(image).scheme

    raw: bytes | None = None
    mime_type: str | None = None
    saved_path: str | None = None
    """本次运行新保存到沙箱工作区的相对路径（仅QQ图片拉取时）"""

    if scheme in ("http", "https"):
        try:
            r = await HTTPX_CLIENT.get(image)
        except HTTPError as e:
            logger.exception("下载图片失败: {}", e)
            return f"无法查看图片，下载失败: {e}"
        if r.status_code != 200:
            return f"无法查看图片，URL: {image}，状态码: {r.status_code}"
        raw = r.content
        mime_type = r.headers.get("Content-Type", "application/octet-stream").split(";")[0]

    elif not scheme:
        sandbox = get_sandbox(ctx)
        if sandbox is not None:
            try:
                raw = (await sandbox.read(Path(image))).read()
            except FileNotFoundError:
                raw = None
        if raw is None:
            # 工作区没有该文件：按QQ消息图片名经 get_image 拉取
            if (qq_raw := await _load_qq_image(ctx, image)) is None:
                if sandbox is None:
                    return f"未找到网络URL、沙箱工作区文件或QQ图片: {image}"
                return f"沙箱工作区中不存在该文件，也不是可获取的QQ图片: {image}"
            raw = qq_raw
            name = Path(image).name or "image"
            if sandbox is not None:
                target = Path("images") / name
                try:
                    await sandbox.write(target, BytesIO(raw))
                    saved_path = str(target)
                except Exception as e:
                    logger.warning("保存QQ图片到沙箱工作区失败: {}", e)
        mime_type = magic.from_buffer(raw[:64], mime=True)

    else:
        return f"不支持的图片来源: {image}"

    if mime_type is None:
        return f"无法查看图片: {image}"

    data = base64.b64encode(raw).decode()
    saved_note = f"（已保存到沙箱工作区 {saved_path}）" if saved_path else ""

    if not cfg.agent.vision:
        if not cfg.image_caption:
            return "当前模型不支持视觉输入，且未配置图片转述模型，无法查看图片。"
        caption = await get_image_caption(data, mime_type)
        if not caption:
            return f"图片 {Path(image).name} 无法获取内容的文字描述。{saved_note}".strip()
        return f"图片 {Path(image).name} 的文字描述{saved_note}：\n{caption}"

    return [
        f"图片查看结果{saved_note}",
        BinaryContent(data=raw, media_type=mime_type),
    ]


async def render_html_image(
    ctx: RunContext[ChatDeps],
    html: str = "",
    file_path: str = "",
    file_name: str = "",
    viewport_width: int = 1280,
    viewport_height: int = 720,
    wait_ms: int = 0,
    full_page: bool = True,
) -> str:
    """将HTML内容或HTML文件渲染为PNG图片并保存到沙箱工作区的rendered/目录，返回沙箱内路径。

    Args:
        html: 要渲染的完整HTML内容；与file_path二选一，优先于file_path。
        file_path: 沙箱工作区内HTML文件的相对路径，无需协议前缀；html为空时使用。
            HTML内的相对资源路径（图片、iframe等）以沙箱工作区根为基准解析。
        file_name: 保存使用的PNG文件名，仅文件名本身；缺省时自动生成。
        viewport_width: 渲染视口宽度，单位像素，默认1280。
        viewport_height: 渲染视口高度，单位像素，默认720。
        wait_ms: networkidle后额外等待的毫秒数，默认0。
        full_page: 是否截图整个页面，默认True。
    """
    sandbox = get_sandbox(ctx)
    if sandbox is None:
        return "未启用沙箱，无法渲染HTML为图片。"

    if not html:
        if not file_path:
            return "未提供HTML内容或文件路径，无法渲染为图片。"
        try:
            stream = await sandbox.read(Path(file_path))
        except FileNotFoundError:
            return f"沙箱工作区中不存在该文件: {file_path}"
        html = stream.read().decode("utf-8", errors="replace")

    # 文件名必须纯净，防止借助文件名做路径穿越
    if Path(file_name).name != file_name:
        return f"文件名不合法（不能包含路径部分）: {file_name}"
    file_name = file_name or f"html_{uuid.uuid4().hex[:12]}.png"
    if Path(file_name).suffix.lower() != ".png":
        file_name += ".png"

    try:
        app = get_default_application()
        async with app.extensions.playwright.page(
            viewport={"width": viewport_width, "height": viewport_height},
            device_scale_factor=2.0,
        ) as page:
            # 先导航到工作区目录建立相对资源基准，再注入HTML
            await page.goto(f"file://{sandbox.workspace_dir.resolve()}")
            await page.set_content(html, wait_until="networkidle")
            if wait_ms:
                await page.wait_for_timeout(wait_ms)
            image = await page.screenshot(full_page=full_page, type="png")
    except Exception as e:
        logger.exception("HTML渲染为图片失败: {}", e)
        return f"HTML渲染为图片失败: {e}"

    target = Path("rendered") / file_name
    try:
        await sandbox.write(target, BytesIO(image))
    except Exception as e:
        logger.exception("写入沙箱工作区失败: {}", e)
        return f"保存图片到沙箱失败: {e}"

    logger.info("HTML已渲染为图片: {}", target)
    return f"HTML已渲染为图片并保存到沙箱工作区 {target}（{len(image)}字节）"


baidu_client = BaiDu(timeout=60)


async def image_search(ctx: RunContext[ChatDeps], image: str) -> str:
    """以图搜图。给定一张图片，搜索全网相同与相似图片，
    返回完全相同图片与相似图片列表（标题、来源网页、预览地址）。

    Args:
        image: 要搜索的图片来源。支持http/https网络URL；启用沙箱时也支持沙箱工作区内的相对路径。
    """
    source = image
    scheme = urlparse(source).scheme
    url: str | None = None
    file: bytes | None = None
    if scheme in ("http", "https"):
        url = source
    elif not scheme:
        sandbox = get_sandbox(ctx)
        if sandbox is None:
            return f"仅支持http/https网络URL（未启用沙箱，不支持本地路径）: {source}"
        try:
            stream = await sandbox.read(Path(source))
        except FileNotFoundError:
            return f"沙箱工作区中不存在该文件: {source}"
        file = stream.read()
    else:
        return f"不支持的图片来源: {source}"

    try:
        resp = await baidu_client.search(url=url, file=file)
    except Exception as e:
        logger.exception("以图搜图请求失败: {}", e)
        return f"以图搜图请求失败: {e}"

    lines = [f"以图搜图成功，搜索结果页: {resp.url}"]
    if resp.exact_matches:
        lines.append(f"\n完全相同的图片，共{len(resp.exact_matches)}条（最多展示10条）：")
        for i, item in enumerate(resp.exact_matches[:10], 1):
            lines.append(
                f"{i}. 标题: {item.title or '(无标题)'}\n"
                f"   来源网页: {item.url}\n"
                f"   预览: {item.thumbnail}"
            )

    if resp.raw:
        lines.append(f"\n相似图片，共{len(resp.raw)}条（最多展示10条）：")
        for i, item in enumerate(resp.raw[:10], 1):
            lines.append(
                f"{i}. 标题: {item.title or '(无标题)'}\n"
                f"   来源网页: {item.url}\n"
                f"   预览: {item.thumbnail}"
            )

    if not resp.raw and not resp.exact_matches:
        return "以图搜图完成，但未找到相同或相似图片。"
    return "\n".join(lines)


async def send_image(ctx: RunContext[ChatDeps], image: str) -> str:
    """将图片发送给当前会话。

    Args:
        image: 图片来源。支持http/https网络URL；启用沙箱时也支持沙箱工作区内的相对路径。
    """
    source = image
    scheme = urlparse(source).scheme
    if scheme in ("http", "https"):
        segment = MessageSegment.image(source)
    elif not scheme:
        sandbox = get_sandbox(ctx)
        if sandbox is None:
            return f"仅支持http/https网络URL（未启用沙箱，不支持本地路径）: {source}"
        try:
            stream = await sandbox.read(Path(source))
        except FileNotFoundError:
            return f"沙箱工作区中不存在该文件: {source}"
        segment = MessageSegment.image(stream.read())
    else:
        return f"不支持的图片来源: {source}"

    error = await send_to_session(ctx, Message(segment))
    if error:
        return error
    return f"图片已发送给会话 {ctx.deps.session_info.session_id}。"
