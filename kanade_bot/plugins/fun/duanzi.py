import json
import random
from pathlib import Path

from nonebot import get_driver, get_plugin_config, logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from .config import Config

cfg = get_plugin_config(Config)

FACE_IDS = [424, 297]

duanzi_list: list[str] = []


def list_paged_duanzi(page: int = 1) -> str:
    """返回一个简单的段子列表字符串，每个段子占一行，前面有序号"""
    if not duanzi_list:
        return "列表为空"

    total = len(duanzi_list)
    total_pages = (total - 1) // 10 + 1
    if page < 1 or page > total_pages:
        return f"页码错误，当前列表共有{total_pages}页"

    start = (page - 1) * 10
    end = min(start + 10, total)

    messages: list[str] = ["段子/史列表："]
    for i, duanzi in enumerate(duanzi_list[start:end], start=start + 1):
        duanzi = duanzi.replace("\n", " ")
        if len(duanzi) > 13:
            duanzi = duanzi[:10] + "..."
        messages.append(f"{i}. {duanzi}")
    messages.append(f"共{total}条，{page}/{total_pages}页")
    return "\n".join(messages)


def duanzi_to_onebot_message(
    duanzi: str,
    *,
    node_threshold: int = 500,
    chaos_face: bool = False,
    custom_face_id: int | None = None,
) -> Message:
    """将一个段子转换为OneBot消息，支持分段和表情"""
    face_ids = FACE_IDS
    if custom_face_id is not None:
        face_ids = FACE_IDS.copy()
        face_ids.append(custom_face_id)

    message = Message()
    segments = duanzi.split("{{face}}")
    if chaos_face:
        # 如果开启混乱表情，每次都选一个随机的表情
        for i, segment in enumerate(segments):
            if segment:
                message += segment
            if i != len(segments) - 1:
                face_id = random.choice(face_ids)
                message += MessageSegment.face(face_id)
    else:
        # 否则仅抽取一次表情，或直接使用给定表情
        face_id = custom_face_id or random.choice(face_ids)
        for i, segment in enumerate(segments):
            if segment:
                message += segment
            if i != len(segments) - 1:
                message += MessageSegment.face(face_id)

    if len(duanzi) <= node_threshold:
        return message

    node_custom = MessageSegment.node_custom(
        user_id=cfg.bot_id,
        nickname=cfg.bot_nickname,
        content=duanzi,
    )
    return Message(node_custom)


def get_or_random_duanzi(index: int | None = None) -> str | None:
    """从段子列表中随机返回一个段子，如果指定了index则返回对应的段子"""
    if not duanzi_list:
        return None

    if index is not None:
        if index < 1 or index > len(duanzi_list):
            return None
        return duanzi_list[index - 1]

    return random.choice(duanzi_list)


def add_duanzi(duanzi: str) -> bool:
    """向段子列表中添加一个段子，并保存到文件"""
    duanzi_list.append(duanzi)
    duanzi_path = Path(cfg.fun_duanzi_list_file_path)
    duanzi_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with duanzi_path.open("w", encoding="utf-8") as f:
            json.dump(duanzi_list, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存段子列表失败: {e}")
        return False
    return True


def remove_duanzi(index: int) -> bool:
    """删除指定索引的段子，返回是否成功"""
    if index < 1 or index > len(duanzi_list):
        return False

    del duanzi_list[index - 1]

    duanzi_path = Path(cfg.fun_duanzi_list_file_path)
    duanzi_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with duanzi_path.open("w", encoding="utf-8") as f:
            json.dump(duanzi_list, f, ensure_ascii=False, indent=4)
    except Exception as e:
        logger.error(f"保存段子列表失败: {e}")
        return False
    return True


driver = get_driver()


@driver.on_startup
def load_duanzi_list():
    global duanzi_list
    try:
        with open(cfg.fun_duanzi_list_file_path, "r", encoding="utf-8") as f:
            duanzi_list = json.load(f)
    except Exception as e:
        logger.error(f"加载段子列表失败: {e}")
