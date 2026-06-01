import hashlib
from pathlib import Path
from typing import override

from nonebot import get_driver, logger
from pydantic import BaseModel, Field
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from kanade_bot.utils.common import Ptr


def md5(p: Path):
    return hashlib.md5(p.read_bytes()).hexdigest()


def md5_str(s: str):
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class FileSyncedModel(BaseModel):
    """
    支持从文件加载、显式保存到文件的模型。

    用法：
    >>> # 从文件加载（文件不存在时用默认值并保存）
        model = MyModel.from_file(Path("config.json"))
        # 修改字段
        model.name = "new_name"
        # 显式保存到文件
        model.save()
    """

    file_path: Path | None = Field(default=None, exclude=True)
    """绑定的文件路径，用于保存和哈希计算"""

    file_hash: str = Field(default="", exclude=True)
    """文件的 MD5 哈希，用于检测外部变更"""

    @classmethod
    def from_file(cls, file_path: Path):
        """从 JSON 文件加载模型，文件不存在时使用默认值并保存"""
        if not file_path.exists():
            logger.warning(f"模型文件 {file_path} 不存在，使用默认值并保存到文件")
            model = cls(file_path=file_path)
            model.save()  # 保存默认模型到文件
            return model

        model = cls.model_validate_json(file_path.read_text(encoding="utf-8"))
        # 设置文件信息并更新哈希（避免后续监听误判）
        model.file_path = file_path
        model.file_hash = md5(file_path)
        return model

    def save(self):
        """将当前模型保存为 JSON 文件。"""
        p = self.file_path
        if p is None:
            raise ValueError("未指定保存路径，请设置 file_path 字段")

        config_json = self.model_dump_json(indent=2, ensure_ascii=False)
        self.file_hash = md5_str(config_json)  # 更新哈希以反映当前内容

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(config_json, encoding="utf-8")


class ModelReloadHandler[M: FileSyncedModel](FileSystemEventHandler):
    """监听模型文件的修改事件，自动重新加载模型并更新指针"""

    def __init__(self, model_ptr: Ptr[M]):
        super().__init__()
        self._model_ptr = model_ptr

    @override
    def on_modified(self, event):
        if event.is_directory:
            return

        src_path = event.src_path
        if not isinstance(src_path, str):
            src_path = src_path.decode()  # pyright: ignore[reportAttributeAccessIssue]

        model = self._model_ptr.v
        file_path = model.file_path
        if file_path is None or not file_path.samefile(src_path):
            return

        new_hash = md5(file_path)
        if new_hash == model.file_hash:
            return

        logger.info(f"模型文件 {file_path} 已修改，重新加载模型")
        new_model = model.from_file(file_path)
        self._model_ptr.v = new_model


observer = Observer()


def watch_file(file_path: Path, handler: ModelReloadHandler):
    """监控指定的文件，当文件被修改时触发事件处理器"""
    logger.info(f"开始监控模型文件 {file_path}")
    observer.schedule(handler, str(file_path.parent))


driver = get_driver()


@driver.on_startup
def start_watchdog():
    observer.start()
    logger.info("模型文件监控已启动")


@driver.on_shutdown
def stop_watchdog():
    observer.stop()
    observer.join()
    logger.info("模型文件监控已停止")
