import asyncio
from pathlib import Path

from kanade_bot.utils.persistence import DeferredWriter, atomic_write_text


def test_atomic_write_text_replaces_file(tmp_path: Path):
    path = tmp_path / "state.json"
    path.write_text("old", encoding="utf-8")
    atomic_write_text(path, "new")
    assert path.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_deferred_writer_coalesces_updates():
    calls: list[int] = []

    async def run():
        writer = DeferredWriter(lambda: calls.append(1), delay=0.01)
        writer.mark_dirty()
        writer.mark_dirty()
        writer.mark_dirty()
        await asyncio.sleep(0.03)

    asyncio.run(run())
    assert calls == [1]


def test_deferred_writer_flushes_without_running_loop():
    calls: list[int] = []
    writer = DeferredWriter(lambda: calls.append(1))
    writer.mark_dirty()
    assert calls == [1]
