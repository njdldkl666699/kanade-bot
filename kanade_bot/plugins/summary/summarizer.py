from collections import deque
from datetime import datetime
from pathlib import Path

from copilot import CopilotClient, CopilotSession
from copilot.client import StopError
from copilot.session import PermissionHandler, SystemMessageConfig
from nonebot import get_driver, get_plugin_config, logger

from .config import Config

cfg = get_plugin_config(Config)


class Summarizer:
    """总结器类，负责管理总结会话和生成总结"""

    system_prompt_path = Path(cfg.summary_system_prompt_path)
    system_prompt = "总结以下对话内容，提取关键信息并生成简洁的总结：\n\n"
    if not system_prompt_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {system_prompt_path.absolute()}")
    else:
        system_prompt = system_prompt_path.read_text(encoding="utf-8")

    system_message: SystemMessageConfig = {
        "mode": "replace",
        "content": system_prompt,
    }

    SESSION_CONFIG = {
        "on_permission_request": PermissionHandler.approve_all,
        "model": cfg.summary_model,
        "reasoning_effort": "medium",
        "system_message": system_message,
    }

    def __init__(self):
        self._client = CopilotClient()
        """Copilot客户端对象，负责与Copilot服务进行通信，创建和恢复会话等操作"""

        self._message_records: dict[str, deque[str]] = {}
        """记录每个会话的消息历史，用于生成总结

        键为 session_id，值为该会话的消息历史
        """

    def add_message(self, session_id: str, message: str):
        """添加消息到指定会话的消息记录中

        :param session_id: 会话ID
        :param message: 要添加的消息文本
        """
        if session_id not in self._message_records:
            self._message_records[session_id] = deque(maxlen=cfg.summary_max_size)
        self._message_records[session_id].append(message)

    def session_summarizable(self, session_id: str, size: int) -> bool:
        """检查指定会话是否有足够的消息记录可供总结

        :param session_id: 会话ID
        :param size: 要总结的消息条数
        """
        if session_id not in self._message_records:
            return False
        if len(self._message_records[session_id]) < size:
            return False
        return True

    async def summarize(
        self,
        session_id: str,
        size: int,
        *,
        is_group: bool = False,
        group_or_user_name: str | None = None,
        timeout: float = 60,
    ) -> str | None:
        """发送消息并等待响应，返回响应文本

        :param session_id: 会话ID
        :param size: 要总结的消息条数
        :returns: 模型生成的总结文本，如果没有足够的消息记录或发生错误，则返回 None
        """
        if session_id not in self._message_records:
            return None
        messages = self._message_records[session_id]
        if len(messages) < size:
            return None

        if is_group:
            prefix = f"群聊 {group_or_user_name}: \n\n"
        else:
            prefix = f"私聊 {group_or_user_name}: \n\n"

        prompt = prefix + "\n\n".join(messages)
        copilot_session_id = f"summary-{session_id}-{int(datetime.now().timestamp())}"

        session: CopilotSession | None = None
        try:
            session = await self._client.create_session(
                session_id=copilot_session_id, **self.SESSION_CONFIG
            )
            session_event = await session.send_and_wait(prompt, timeout=timeout)
            await session.disconnect()
        except Exception as e:
            logger.error(f"总结会话{session_id}发生错误: {e}")
            session_event = None

        if not session_event:
            return None
        content = session_event.data.content
        if isinstance(content, str):
            return content

        logger.warning(f"总结会话{session_id}的响应内容不是字符串: {content}")
        return None


summarizer = Summarizer()


driver = get_driver()


@driver.on_startup
async def startup():
    await summarizer._client.start()
    logger.info("总结器 Copilot客户端已启动")


@driver.on_shutdown
async def shutdown():
    try:
        await summarizer._client.stop()
    except* StopError as eg:
        logger.warning(f"停止总结器 Copilot客户端时发生错误: {eg.message}")
    logger.info("总结器 Copilot客户端已关闭")
