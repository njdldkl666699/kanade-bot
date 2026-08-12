import asyncio
import importlib
import subprocess
from pathlib import Path


def test_random_clip_audio_uses_partial_ffmpeg_transcode(tmp_path: Path):
    source = tmp_path / "source.mp3"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=20",
            str(source),
        ],
        check=True,
    )

    # The module needs NoneBot config at import time, so load it through a minimal app.
    import nonebot

    nonebot.init(_env_file=None)
    if nonebot.get_plugin("nonebot_plugin_localstore") is None:
        assert nonebot.load_plugin("nonebot_plugin_localstore")
    module = importlib.import_module("kanade_bot.plugins.music.audio")

    encoded = asyncio.run(module.random_clip_audio(source))
    assert encoded.startswith((b"ID3", b"\xff"))

    clip = tmp_path / "clip.mp3"
    clip.write_bytes(encoded)
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(clip),
        ],
        stdout=subprocess.PIPE,
        check=True,
    )
    duration = float(result.stdout)
    assert 4.9 <= duration <= 15.2
