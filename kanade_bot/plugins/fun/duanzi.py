import json
from pathlib import Path
import random

from nonebot import get_driver, get_plugin_config, logger

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


def get_random_duanzi() -> str | None:
    """从段子列表中随机返回一个段子"""
    if not duanzi_list:
        return None

    return random.choice(duanzi_list)


def add_duanzi(duanzi: str) -> bool:
    """向段子列表中添加一个段子，并保存到文件"""
    duanzi_list.append(duanzi)
    duanzi_path = Path(cfg.fun_duanzi_list_path)
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

    duanzi_path = Path(cfg.fun_duanzi_list_path)
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
        with open(cfg.fun_duanzi_list_path, "r", encoding="utf-8") as f:
            duanzi_list = json.load(f)
    except Exception as e:
        logger.error(f"加载段子列表失败: {e}")
