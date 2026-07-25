import importlib
import shutil
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import nonebot
from PIL import Image, ImageDraw

nonebot.init(_env_file=None)
assert nonebot.load_plugin("kanade_bot.plugins.model_updater")
assert nonebot.load_plugin("kanade_bot.plugins.command_counter")
assert nonebot.load_plugin("kanade_bot.plugins.gallery")

gallery = importlib.import_module("kanade_bot.plugins.gallery.gallery")


class FakeGalleryNameData:
    def __init__(
        self,
        iota: int = 0,
        name_to_aliases: dict[str, list[str]] | None = None,
    ):
        self.instance = SimpleNamespace(
            iota=iota,
            name_to_aliases=name_to_aliases or {},
        )
        self.save_count = 0

    def save_to_file(self) -> None:
        self.save_count += 1


def _make_test_image(
    path: Path, *, inverted: bool = False, compress_level: int = 6
) -> None:
    background = "white" if not inverted else "black"
    foreground = "#155eef" if not inverted else "#fdb022"
    image = Image.new("RGB", (320, 240), background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((20, 20, 150, 100), fill=foreground)
    draw.ellipse((170, 70, 300, 210), fill="#d92d20")
    draw.line((0, 230, 310, 10), fill="#039855", width=9)
    image.save(path, compress_level=compress_level)


def _configure_gallery(
    monkeypatch,
    tmp_path: Path,
    *,
    iota: int = 0,
    name_to_aliases: dict[str, list[str]] | None = None,
):
    name_data = FakeGalleryNameData(
        iota=iota,
        name_to_aliases=name_to_aliases,
    )
    monkeypatch.setattr(gallery, "cfg", SimpleNamespace(data_dir_path=tmp_path))
    monkeypatch.setattr(gallery, "gallery_name_data", name_data)
    return name_data


def test_add_pictures_skips_exact_duplicate_and_saves_unique(monkeypatch, tmp_path):
    name_data = _configure_gallery(monkeypatch, tmp_path, iota=42)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    existing = gallery_dir / "42.png"
    duplicate = tmp_path / "duplicate.png"
    unique = tmp_path / "unique.png"
    _make_test_image(existing)
    shutil.copy(existing, duplicate)
    _make_test_image(unique, inverted=True)

    result = gallery.add_pictures("test", [duplicate, unique])

    assert result.added_count == 1
    assert len(result.duplicates) == 1
    assert result.duplicates[0].existing_path == existing
    assert result.duplicates[0].reason == "Exact match"
    assert (gallery_dir / "43.png").is_file()
    assert result.duplicate_image is not None
    with Image.open(BytesIO(result.duplicate_image)) as comparison:
        assert comparison.format == "PNG"
        assert comparison.width > comparison.height
    assert name_data.save_count == 1


def test_add_pictures_detects_perceptual_duplicate(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path, iota=1)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    existing = gallery_dir / "1.png"
    candidate = tmp_path / "candidate.png"
    _make_test_image(existing, compress_level=0)
    _make_test_image(candidate, compress_level=9)
    assert existing.read_bytes() != candidate.read_bytes()

    result = gallery.add_pictures("test", [candidate])

    assert result.added_count == 0
    assert len(result.duplicates) == 1
    assert result.duplicates[0].reason == "dHash + pHash + aHash"


def test_add_pictures_force_bypasses_duplicate_check(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path, iota=7)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    existing = gallery_dir / "7.png"
    candidate = tmp_path / "candidate.png"
    _make_test_image(existing)
    shutil.copy(existing, candidate)

    result = gallery.add_pictures("test", [candidate], force=True)

    assert result.added_count == 1
    assert result.duplicates == []
    assert result.duplicate_image is None
    assert (gallery_dir / "8.png").is_file()


def test_add_pictures_creates_missing_gallery_directory(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    candidate = tmp_path / "candidate.png"
    _make_test_image(candidate)

    result = gallery.add_pictures("test", [candidate])

    assert result.added_count == 1
    assert (tmp_path / "test" / "1.png").is_file()


def test_gallery_overview_uses_smallest_picture_id_as_cover(monkeypatch, tmp_path):
    _configure_gallery(
        monkeypatch,
        tmp_path,
        name_to_aliases={
            "有图画廊": ["别名一", "别名二"],
            "空画廊": [],
            "目录缺失": ["missing"],
        },
    )
    populated_dir = tmp_path / "有图画廊"
    populated_dir.mkdir()
    _make_test_image(populated_dir / "10.png", inverted=True)
    _make_test_image(populated_dir / "2.png")
    (tmp_path / "空画廊").mkdir()

    items = gallery.get_gallery_overview_items()

    assert [item.name for item in items] == ["有图画廊", "空画廊", "目录缺失"]
    assert items[0].cover_path == populated_dir / "2.png"
    assert items[0].picture_count == 2
    assert items[0].aliases == ["别名一", "别名二"]
    assert items[1].cover_path is None
    assert items[1].picture_count == 0
    assert items[2].cover_path is None
    assert items[2].picture_count == 0


def test_render_gallery_overview_returns_png(monkeypatch, tmp_path):
    _configure_gallery(
        monkeypatch,
        tmp_path,
        name_to_aliases={
            "画廊名称很长需要自动换行": ["很长的别名一", "很长的别名二"],
            "空画廊": [],
        },
    )
    gallery_dir = tmp_path / "画廊名称很长需要自动换行"
    gallery_dir.mkdir()
    _make_test_image(gallery_dir / "1.png")
    (tmp_path / "空画廊").mkdir()

    overview = gallery.render_gallery_overview()

    with Image.open(BytesIO(overview)) as image:
        assert image.format == "PNG"
        assert image.width > image.height


def test_render_gallery_overview_returns_empty_for_no_galleries(
    monkeypatch,
    tmp_path,
):
    _configure_gallery(monkeypatch, tmp_path)

    assert gallery.render_gallery_overview() == b""
