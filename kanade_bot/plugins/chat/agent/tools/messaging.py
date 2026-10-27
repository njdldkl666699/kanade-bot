"""消息类工具：查看转发记录、发送语音与文件"""

import re
from pathlib import Path

from httpx import AsyncClient, HTTPError
from nonebot import get_bot, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from pydantic_ai import RunContext

from kanade_bot.utils.onebot11 import upload_group_file, upload_private_file
from kanade_bot.utils.parse import format_forward_message_for_ai

from ...config import cfg
from ..deps import ChatDeps
from .common import get_sandbox

_FORWARD_MARKER_RE = re.compile(r"\[forward:\s*id=([^\]\s]+)\s*\]")
"""消息文本中合并转发标记 `[forward:id=xxx]` 的匹配模式"""

tts_client = AsyncClient(base_url=cfg.tts.base_url or "", timeout=180)


async def send_to_session(ctx: RunContext[ChatDeps], message: Message) -> str | None:
    """向当前会话发送OneBot消息，返回错误信息（None表示成功）"""
    info = ctx.deps.session_info
    try:
        bot = get_bot(ctx.deps.bot_id)
    except (KeyError, ValueError):
        logger.error("无法获取Bot实例，bot_id: {}", ctx.deps.bot_id)
        return "无法获取Bot实例，无法发送消息。"
    if not isinstance(bot, Bot):
        return "当前类型的Bot不支持发送此类消息。"

    try:
        if group_id := info.group_id:
            await bot.send_msg(message=message, group_id=int(group_id), message_type="group")
        elif user_id := info.user_id:
            await bot.send_msg(message=message, user_id=int(user_id), message_type="private")
        else:
            return "当前会话没有可用的用户ID或群组ID，无法发送消息。"
    except Exception as e:
        # 网络断连、ActionFailed等：转为错误文本返回给模型，不中断整个运行
        logger.exception("发送OneBot消息失败: {}", e)
        return f"发送消息失败: {e}"
    return None


async def get_forward_message(ctx: RunContext[ChatDeps], forward_id: str) -> str:
    """获取一条合并转发消息的完整内容。

    消息中的合并转发会显示为`[forward:id=xxx]`标记，传入其中的 id 即可递归展开全部子消息。
    子消息中的图片仅显示`[图片：文件名]`占位。

    Args:
        forward_id: 转发消息id
    """
    # 容错：模型偶尔会把整个标记当 id 传入
    if m := _FORWARD_MARKER_RE.fullmatch(forward_id.strip()):
        forward_id = m.group(1)

    try:
        bot = get_bot(ctx.deps.bot_id)
    except (KeyError, ValueError):
        logger.error("无法获取Bot实例，bot_id: {}", ctx.deps.bot_id)
        return "无法获取Bot实例，无法获取转发消息。"
    if not isinstance(bot, Bot):
        return "当前平台不支持获取合并转发消息。"

    logger.info("获取转发消息工具被调用，id: {}", forward_id)
    try:
        text, _ = await format_forward_message_for_ai(
            bot,
            MessageSegment("forward", {"id": forward_id}),
            # 子消息图片不拉取字节，保留占位给 view_image 按需查看
            fetch_images=False,
        )
    except Exception as e:
        logger.exception("获取转发消息失败: {}", e)
        return f"获取转发消息失败: {e}"
    if text == f"<forward id={forward_id}>\n</forward>":
        return f"未获取到转发消息 {forward_id} 的内容，消息可能已过期或不可用。"
    return text


async def send_voice(ctx: RunContext[ChatDeps], text: str) -> str:
    """向当前会话发送一段语音。将文本转换为语音后，自动返回给当前会话。

    Args:
        text: 要发送为语音的文本内容。
    """
    # OpenAI Speech接口
    try:
        r = await tts_client.post(
            "/v1/audio/speech",
            headers={"Content-Type": "application/json"},
            json={
                "input": text,
                "model": cfg.tts.model,
                "voice": cfg.tts.voice,
            },
        )
    except HTTPError as e:
        logger.exception("文本转语音请求失败: {}", e)
        return f"文本转语音请求失败: {e}"
    if r.status_code != 200:
        return f"文本转语音请求失败，状态码: {r.status_code}"

    error = await send_to_session(ctx, Message(MessageSegment.record(r.content)))
    if error:
        return error
    return f"当前文本已转换为语音并发送给会话 {ctx.deps.session_info.session_id}。"


async def send_file(ctx: RunContext[ChatDeps], path: str) -> str:
    """将沙箱工作区中的文件发送给当前会话。

    Args:
        path: 沙箱工作区内的文件相对路径。
    """
    sandbox = get_sandbox(ctx)
    if sandbox is None:
        return "未启用沙箱，无法发送文件。"

    local_path = (sandbox.workspace_dir / path).resolve()
    if not local_path.is_relative_to(sandbox.workspace_dir):
        return f"路径不合法（不能越出沙箱工作区）: {path}"
    if not local_path.is_file():
        return f"沙箱工作区中不存在该文件: {path}"

    file_name = Path(path).name
    info = ctx.deps.session_info
    try:
        bot = get_bot(ctx.deps.bot_id)
    except (KeyError, ValueError):
        logger.error("无法获取Bot实例，bot_id: {}", ctx.deps.bot_id)
        return "无法获取Bot实例，无法发送文件消息。"
    if not isinstance(bot, Bot):
        return "当前类型的Bot不支持发送文件消息。"

    try:
        if group_id := info.group_id:
            await upload_group_file(bot, group_id=int(group_id), file_path=local_path)
        elif user_id := info.user_id:
            await upload_private_file(bot, user_id=int(user_id), file_path=local_path)
        else:
            return "当前会话没有可用的用户ID或群组ID，无法发送文件消息。"
    except Exception as e:
        # 上传接口断连/风控拒绝等：转为错误文本返回给模型，不中断整个运行
        logger.exception("发送文件失败: {}", e)
        return f"发送文件失败: {e}"

    return f"文件 {file_name} 已发送给会话 {ctx.deps.session_info.session_id}。"
