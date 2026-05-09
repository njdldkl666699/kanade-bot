import json
from collections import deque
from datetime import datetime
from pathlib import Path

from copilot import CopilotSession
from copilot.generated.session_events import AssistantMessageData
from copilot.session import PermissionHandler, SystemMessageConfig
from nonebot import get_plugin_config, logger

from kanade_bot.utils.common import COPILOT_CLIENT

from .config import Config

cfg = get_plugin_config(Config)


class Summarizer:
    """总结器类，负责管理总结会话和生成总结"""

    system_prompt_path = Path(cfg.summary_system_prompt_file_path)
    system_prompt = "总结以下对话内容，提取关键信息并生成简洁的总结：\n\n"
    if not system_prompt_path.is_file():
        logger.warning(f"系统提示词文件不存在，路径: {system_prompt_path.absolute()}")
    else:
        system_prompt = system_prompt_path.read_text(encoding="utf-8")
        system_prompt = system_prompt.replace("{{summary_bot_name}}", cfg.summary_bot_name)

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

    def load_message_records(self):
        """从缓存文件中加载历史消息记录到内存中"""
        cache_path = Path(cfg.summary_message_records_file_path)
        if not cache_path.is_file():
            logger.info(f"总结缓存文件不存在，路径: {cache_path.absolute()}")
            return

        with cache_path.open("r", encoding="utf-8") as f:
            data: dict[str, list[str]] = json.load(f)
        for session_id, messages in data.items():
            self._message_records[session_id] = deque(messages, maxlen=cfg.summary_max_size)

    def save_message_records(self):
        """将当前的消息记录缓存保存到文件中"""
        cache_path = Path(cfg.summary_message_records_file_path)
        cache_path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            session_id: list(messages) for session_id, messages in self._message_records.items()
        }
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

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
        :param size: 要总结的消息条数，不足则总结全部
        :returns: 模型生成的总结文本，如果发生错误，则返回 None
        """
        if session_id not in self._message_records:
            return None
        # 获取要总结的消息切片，取最后 size 条记录
        messages_slice = list(self._message_records[session_id])[-size:]

        if is_group:
            prefix = f"群聊 {group_or_user_name}: \n\n"
        else:
            prefix = f"私聊 {group_or_user_name}: \n\n"

        prompt = prefix + "\n\n".join(messages_slice)
        copilot_session_id = f"summary-{session_id}-{int(datetime.now().timestamp())}"

        session: CopilotSession | None = None
        try:
            session = await COPILOT_CLIENT.create_session(
                session_id=copilot_session_id, **self.SESSION_CONFIG
            )
            session_event = await session.send_and_wait(prompt, timeout=timeout)
            await session.disconnect()
        except Exception as e:
            logger.error(f"总结会话{session_id}发生错误: {e}")
            session_event = None

        if not session_event:
            return None
        if not isinstance(session_event.data, AssistantMessageData):
            logger.warning(f"总结会话{session_id}的响应内容不是文本，数据: {session_event.data}")
            return None

        return session_event.data.content


SUMMARIZER = Summarizer()
