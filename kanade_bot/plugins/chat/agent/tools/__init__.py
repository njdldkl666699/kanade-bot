"""聊天 Agent 宿主工具集"""

from typing import Any

from .env import setup_python_env
from .media import image_search, render_html_image, send_image, view_image
from .memory import forget_memory, recall_memory, save_memory
from .messaging import get_forward_message, send_file, send_voice
from .scheduling import schedule_task


def build_tools() -> list[Any]:
    """构建聊天Agent的静态工具列表"""
    return [
        view_image,
        get_forward_message,
        save_memory,
        recall_memory,
        forget_memory,
        send_voice,
        image_search,
        render_html_image,
        send_image,
        send_file,
        setup_python_env,
        schedule_task,
    ]
