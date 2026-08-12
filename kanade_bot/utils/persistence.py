import asyncio
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import RLock


def atomic_write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Write a text file completely, then atomically replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class DeferredWriter:
    """Coalesce frequent updates while guaranteeing an explicit final flush."""

    def __init__(self, write: Callable[[], None], *, delay: float = 1.0):
        self._write = write
        self._delay = delay
        self._dirty = False
        self._handle: asyncio.TimerHandle | None = None
        self._lock = RLock()

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True
            if self._handle is not None:
                return
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self.flush()
                return
            self._handle = loop.call_later(self._delay, self.flush)

    def flush(self) -> None:
        with self._lock:
            if self._handle is not None:
                self._handle.cancel()
                self._handle = None
            if not self._dirty:
                return
            self._write()
            self._dirty = False
