import importlib
import shutil
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import nonebot
from PIL import Image, ImageDraw

nonebot.init(_env_file=None)
assert nonebot.load_plugin("nonebot_plugin_localstore")
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
    monkeypatch.setattr(
        gallery,
        "cfg",
        SimpleNamespace(
            data_dir_path=tmp_path,
            cache_dir_path=tmp_path / "cache",
        ),
    )
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
    assert result.duplicates[0].reason == "文件完全一致"
    assert "force 参数" in result.summary("test")
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
    assert result.duplicates[0].reason == "感知哈希相似：dHash、pHash、aHash"


def test_duplicate_hash_index_reuses_unchanged_existing_hash(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path, iota=1)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    existing = gallery_dir / "1.png"
    candidate = tmp_path / "candidate.png"
    _make_test_image(existing)
    _make_test_image(candidate, inverted=True)

    gallery.find_duplicate_pictures("test", [candidate])
    original = gallery.calculate_image_hashes

    def fail_for_existing(path: Path):
        if path == existing:
            raise AssertionError("unchanged existing image was hashed again")
        return original(path)

    monkeypatch.setattr(gallery, "calculate_image_hashes", fail_for_existing)
    gallery.find_duplicate_pictures("test", [candidate])


def test_duplicate_hash_index_invalidates_changed_existing_file(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path, iota=1)
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    existing = gallery_dir / "1.png"
    candidate = tmp_path / "candidate.png"
    _make_test_image(existing)
    _make_test_image(candidate, inverted=True)
    gallery.find_duplicate_pictures("test", [candidate])

    _make_test_image(existing, inverted=True, compress_level=0)
    original = gallery.calculate_image_hashes
    hashed_paths: list[Path] = []

    def track(path: Path):
        hashed_paths.append(path)
        return original(path)

    monkeypatch.setattr(gallery, "calculate_image_hashes", track)
    gallery.find_duplicate_pictures("test", [candidate])
    assert existing in hashed_paths


def test_picture_id_index_tracks_added_and_removed_files(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    candidate = tmp_path / "candidate.png"
    _make_test_image(candidate)

    result = gallery.add_pictures("test", [candidate], force=True)
    saved = tmp_path / "test" / "1.png"
    assert result.added_count == 1
    assert gallery.get_picture_by_id(1) == saved

    saved.unlink()
    gallery.remove_picture_from_index(saved)
    assert gallery.get_picture_by_id(1) is None


def test_picture_id_index_miss_does_not_rewrite_unchanged_index(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    save_calls = 0
    original_save = gallery._gallery_index._save

    def track_save():
        nonlocal save_calls
        save_calls += 1
        original_save()

    monkeypatch.setattr(gallery._gallery_index, "_save", track_save)

    assert gallery.get_picture_by_id(999) is None
    assert save_calls == 0


def test_remove_nested_gallery_clears_its_index_entries(monkeypatch, tmp_path):
    _configure_gallery(monkeypatch, tmp_path)
    candidate = tmp_path / "candidate.png"
    _make_test_image(candidate)
    saved = gallery.save_pictures("parent/child", [candidate])[0]
    assert gallery.get_picture_by_id(1) == saved

    gallery.remove_gallery_from_index("parent/child")

    index_data = (tmp_path / "cache" / gallery.HASH_INDEX_FILE_NAME).read_text(
        encoding="utf-8"
    )
    assert "parent/child/1.png" not in index_data
    assert gallery.get_picture_by_id(1) == saved


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


def test_gallery_overview_uses_first_picture_as_cover(monkeypatch, tmp_path):
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
    original_iterdir = Path.iterdir

    def ordered_iterdir(path: Path):
        if path == populated_dir:
            return iter((populated_dir / "10.png", populated_dir / "2.png"))
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", ordered_iterdir)

    items = gallery.get_gallery_overview_items()

    assert [item.name for item in items] == ["有图画廊", "空画廊", "目录缺失"]
    assert items[0].cover_path == populated_dir / "10.png"
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


def test_render_gallery_overview_uses_five_square_covers_and_emoji(
    monkeypatch,
    tmp_path,
):
    _configure_gallery(
        monkeypatch,
        tmp_path,
        name_to_aliases={f"画廊{i}😀": [f"别名{i}❤️"] for i in range(6)},
    )
    emoji_color = (255, 0, 255, 255)
    monkeypatch.setattr(
        gallery,
        "_render_emoji",
        lambda _text, size: Image.new("RGBA", (size, size), emoji_color),
    )

    for i in range(6):
        gallery_dir = tmp_path / f"画廊{i}😀"
        gallery_dir.mkdir()
        _make_test_image(gallery_dir / f"{i + 1}.png")

    overview = gallery.render_gallery_overview()

    assert gallery.OVERVIEW_COLUMNS == 5
    assert gallery.OVERVIEW_COVER_SIZE[0] == gallery.OVERVIEW_COVER_SIZE[1]
    with gallery._load_cover(
        tmp_path / "画廊0😀" / "1.png",
        gallery.OVERVIEW_COVER_SIZE,
    ) as cover:
        assert cover.size == gallery.OVERVIEW_COVER_SIZE
    assert list(gallery._iter_text_units("画廊👨‍👩‍👧‍👦")) == [
        "画",
        "廊",
        "👨‍👩‍👧‍👦",
    ]
    with Image.open(BytesIO(overview)).convert("RGBA") as image:
        assert emoji_color in image.get_flattened_data()


def test_render_gallery_overview_returns_empty_for_no_galleries(
    monkeypatch,
    tmp_path,
):
    _configure_gallery(monkeypatch, tmp_path)

    assert gallery.render_gallery_overview() == b""


def test_render_gallery_overview_uses_cache(monkeypatch, tmp_path):
    _configure_gallery(
        monkeypatch,
        tmp_path,
        name_to_aliases={"test": []},
    )
    gallery_dir = tmp_path / "test"
    gallery_dir.mkdir()
    _make_test_image(gallery_dir / "1.png")

    first_render = gallery.render_gallery_overview()
    cache_path = gallery._render_cache_path()

    assert cache_path.read_bytes() == first_render
    monkeypatch.setattr(
        gallery,
        "get_gallery_overview_items",
        lambda: (_ for _ in ()).throw(AssertionError("overview was rendered again")),
    )
    assert gallery.render_gallery_overview() == first_render


def test_adding_picture_invalidates_overview_and_current_gallery_cache_only(
    monkeypatch,
    tmp_path,
):
    _configure_gallery(
        monkeypatch,
        tmp_path,
        iota=2,
        name_to_aliases={"current": [], "other": []},
    )
    current_dir = tmp_path / "current"
    other_dir = tmp_path / "other"
    current_dir.mkdir()
    other_dir.mkdir()
    _make_test_image(current_dir / "1.png")
    _make_test_image(other_dir / "2.png", inverted=True)

    gallery.render_gallery_overview()
    gallery.render_gallery_thumbnails("current", [current_dir / "1.png"])
    gallery.render_gallery_thumbnails("other", [other_dir / "2.png"])
    overview_cache = gallery._render_cache_path()
    current_cache = gallery._render_cache_path("current")
    other_cache = gallery._render_cache_path("other")
    assert overview_cache.is_file()
    assert current_cache.is_file()
    current_cache_contents = current_cache.read_bytes()
    other_cache_contents = other_cache.read_bytes()
    monkeypatch.setattr(
        gallery,
        "_load_thumbnail",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("gallery thumbnails were rendered again")
        ),
    )
    assert (
        gallery.render_gallery_thumbnails("current", [current_dir / "1.png"])
        == current_cache_contents
    )
    assert (
        gallery.render_gallery_thumbnails("other", [other_dir / "2.png"])
        == other_cache_contents
    )

    candidate = tmp_path / "candidate.png"
    _make_test_image(candidate, inverted=True)
    result = gallery.add_pictures("current", [candidate], force=True)

    assert result.added_count == 1
    assert not overview_cache.exists()
    assert not current_cache.exists()
    assert other_cache.read_bytes() == other_cache_contents
