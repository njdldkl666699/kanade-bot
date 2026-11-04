import importlib
import shutil
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import nonebot
from nonebot.plugin import get_plugin
from PIL import Image, ImageDraw

nonebot.init(_env_file=None)
for _module_name in (
    "nonebot_plugin_localstore",
    "kanade_bot.plugins.model_updater",
    "kanade_bot.plugins.command_counter",
    "kanade_bot.plugins.gallery",
):
    # 同一 pytest 会话中可能已被其他测试文件加载
    if get_plugin(_module_name.rsplit(".", 1)[-1]) is None:
        assert nonebot.load_plugin(_module_name)

gallery = importlib.import_module("kanade_bot.plugins.gallery.gallery")


class FakeGalleryNameData:
    def __init__(self):
        self.instance = SimpleNamespace(
            iota=0,
            name_to_aliases={},
        )
        self.save_count = 0

    def save_to_file(self) -> None:
        self.save_count += 1


def _make_test_image(path: Path, *, inverted: bool = False, compress_level: int = 6) -> None:
    background = "white" if not inverted else "black"
    foreground = "#155eef" if not inverted else "#fdb022"
    image = Image.new("RGB", (320, 240), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 150, 100), fill=foreground)
    draw.ellipse((170, 70, 300, 210), fill="#d92d20")
    draw.line((0, 230, 310, 10), fill="#039855", width=9)
    image.save(path, compress_level=compress_level)


def _configure_gallery(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(
        gallery,
        "cfg",
        SimpleNamespace(
            data_dir_path=tmp_path,
            cache_dir_path=tmp_path / "cache",
        ),
    )
    monkeypatch.setattr(gallery, "gallery_name_data", FakeGalleryNameData())


def test_check_gallery_duplicates_reports_exact_pair(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    first = gallery_dir / "1.png"
    unique = gallery_dir / "2.png"
    second = gallery_dir / "3.png"
    _make_test_image(first)
    _make_test_image(unique, inverted=True)
    shutil.copy(first, second)

    result = gallery.check_gallery_duplicates("test")

    assert result.picture_count == 3
    assert len(result.duplicate_pairs) == 1
    pair = result.duplicate_pairs[0]
    assert pair.first_path == first
    assert pair.second_path == second
    assert pair.reason == "文件完全一致"
    assert "发现 1 对重复图片" in result.summary("test")


def test_check_gallery_duplicates_reports_perceptual_pair(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    first = gallery_dir / "1.png"
    second = gallery_dir / "2.png"
    _make_test_image(first, compress_level=0)
    _make_test_image(second, compress_level=9)
    assert first.read_bytes() != second.read_bytes()

    result = gallery.check_gallery_duplicates("test")

    assert result.picture_count == 2
    assert len(result.duplicate_pairs) == 1
    pair = result.duplicate_pairs[0]
    assert pair.first_path == first
    assert pair.second_path == second
    assert pair.reason == "感知哈希相似：dHash、pHash、aHash"


def test_check_gallery_duplicates_without_duplicates(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    _make_test_image(gallery_dir / "1.png")
    _make_test_image(gallery_dir / "2.png", inverted=True)

    result = gallery.check_gallery_duplicates("test")

    assert result.picture_count == 2
    assert result.duplicate_pairs == []
    assert "未发现重复图片" in result.summary("test")


def test_check_gallery_duplicates_reports_exact_group_as_adjacent_pairs(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    base = gallery_dir / "1.png"
    _make_test_image(base)
    shutil.copy(base, gallery_dir / "2.png")
    shutil.copy(base, gallery_dir / "5.png")

    result = gallery.check_gallery_duplicates("test")

    assert [(pair.first_path.name, pair.second_path.name) for pair in result.duplicate_pairs] == [
        ("1.png", "2.png"),
        ("2.png", "5.png"),
    ]
    assert all(pair.reason == "文件完全一致" for pair in result.duplicate_pairs)


def test_render_gallery_self_check_outputs_png(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    first = gallery_dir / "1.png"
    second = gallery_dir / "2.png"
    _make_test_image(first, compress_level=0)
    _make_test_image(second, compress_level=9)
    result = gallery.check_gallery_duplicates("test")

    image = gallery.render_gallery_self_check("test", result.duplicate_pairs)

    with Image.open(BytesIO(image)) as comparison:
        assert comparison.format == "PNG"
        assert comparison.width > comparison.height
