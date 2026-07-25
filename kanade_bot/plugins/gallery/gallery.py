import shutil
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import cfg, gallery_name_data
from .image_hash import ImageHashes, calculate_image_hashes, perceptual_distances


@dataclass(frozen=True)
class DuplicatePicture:
    candidate_index: int
    candidate_path: Path
    existing_path: Path
    reason: str


@dataclass(frozen=True)
class AddPicturesResult:
    added_count: int
    duplicates: list[DuplicatePicture]
    duplicate_image: bytes | None = None

    def summary(self, gallery_name: str) -> str:
        lines: list[str] = []
        if self.duplicates:
            lines.append(f"检测到 {len(self.duplicates)} 张重复图片，已跳过。")
        lines.append(f"成功添加 {self.added_count} 张图片到画廊 {gallery_name}。")
        return "\n".join(lines)


@dataclass(frozen=True)
class GalleryOverviewItem:
    name: str
    aliases: list[str]
    picture_count: int
    cover_path: Path | None


def get_gallery_name(name_or_alias: str) -> str | None:
    """根据名称或别名获取画廊名称"""
    v = gallery_name_data.instance
    if name_or_alias in v.name_to_aliases:
        return name_or_alias
    return v.alias_to_name.get(name_or_alias)


def get_picture_by_id(pic_id: int) -> Path | None:
    """根据图片id获取图片文件路径"""
    for file in cfg.data_dir_path.rglob(f"{pic_id}.*"):
        if file.is_file():
            return file
    return None


def save_pictures(name: str, pic_paths: list[Path]) -> list[Path]:
    """保存图片文件到画廊目录"""
    gallery_dir = cfg.data_dir_path / name
    gallery_dir.mkdir(parents=True, exist_ok=True)

    v = gallery_name_data.instance
    saved_paths: list[Path] = []
    for pic_path in pic_paths:
        # 生成图片文件名
        pic_id = v.iota + 1
        v.iota = pic_id
        suffix = pic_path.suffix
        new_pic_path = gallery_dir / f"{pic_id}{suffix}"
        shutil.copy(pic_path, new_pic_path)
        saved_paths.append(new_pic_path)

    gallery_name_data.save_to_file()
    return saved_paths


def add_pictures(
    name: str,
    pic_paths: list[Path],
    *,
    force: bool = False,
) -> AddPicturesResult:
    """Add pictures, skipping and rendering matches already in the gallery."""
    if force:
        saved_paths = save_pictures(name, pic_paths)
        return AddPicturesResult(added_count=len(saved_paths), duplicates=[])

    unique_paths, duplicates = find_duplicate_pictures(name, pic_paths)
    saved_paths = save_pictures(name, unique_paths)
    duplicate_image = render_duplicate_comparisons(duplicates) if duplicates else None
    return AddPicturesResult(
        added_count=len(saved_paths),
        duplicates=duplicates,
        duplicate_image=duplicate_image,
    )


def find_duplicate_pictures(
    name: str,
    candidate_paths: list[Path],
) -> tuple[list[Path], list[DuplicatePicture]]:
    """Compare candidate pictures with files that already exist in a gallery."""
    gallery_dir = cfg.data_dir_path / name
    existing_hashes: list[tuple[Path, ImageHashes]] = []
    exact_hashes: dict[str, Path] = {}
    existing_paths = gallery_dir.iterdir() if gallery_dir.is_dir() else ()
    for existing_path in existing_paths:
        if not existing_path.is_file():
            continue
        try:
            hashes = calculate_image_hashes(existing_path)
        except (OSError, ValueError) as e:
            logger.warning(f"无法计算画廊图片 {existing_path} 的哈希，已跳过：{e}")
            continue
        existing_hashes.append((existing_path, hashes))
        exact_hashes.setdefault(hashes.file_hash, existing_path)

    unique_paths: list[Path] = []
    duplicates: list[DuplicatePicture] = []
    for candidate_index, candidate_path in enumerate(candidate_paths, start=1):
        try:
            candidate_hashes = calculate_image_hashes(candidate_path)
        except (OSError, ValueError) as e:
            logger.warning(f"无法计算待添加图片 {candidate_path} 的哈希，将正常添加：{e}")
            unique_paths.append(candidate_path)
            continue

        if existing_path := exact_hashes.get(candidate_hashes.file_hash):
            duplicates.append(
                DuplicatePicture(
                    candidate_index=candidate_index,
                    candidate_path=candidate_path,
                    existing_path=existing_path,
                    reason="Exact match",
                )
            )
            continue

        best_match: tuple[int, Path, tuple[int, int, int]] | None = None
        for existing_path, hashes in existing_hashes:
            distances = perceptual_distances(candidate_hashes, hashes)
            if distances is None:
                continue
            score = sum(distances)
            if best_match is None or score < best_match[0]:
                best_match = (score, existing_path, distances)

        if best_match is None:
            unique_paths.append(candidate_path)
            continue

        _, existing_path, distances = best_match
        similar_hashes = [
            hash_name
            for hash_name, distance, threshold in zip(
                ("dHash", "pHash", "aHash"),
                distances,
                (8, 2, 2),
            )
            if distance < threshold
        ]
        duplicates.append(
            DuplicatePicture(
                candidate_index=candidate_index,
                candidate_path=candidate_path,
                existing_path=existing_path,
                reason=" + ".join(similar_hashes),
            )
        )

    return unique_paths, duplicates


GALLERY_COLUMNS = 10
THUMBNAIL_SIZE = (100, 75)
GALLERY_PADDING = 16
GALLERY_GAP = 8
LABEL_HEIGHT = 24

OVERVIEW_COLUMNS = 4
OVERVIEW_CELL_WIDTH = 220
OVERVIEW_COVER_SIZE = (200, 150)
OVERVIEW_HEADER_HEIGHT = 58
OVERVIEW_TEXT_GAP = 6

DUPLICATE_COLUMNS = 2
DUPLICATE_THUMBNAIL_SIZE = (180, 135)
DUPLICATE_CELL_WIDTH = 408
DUPLICATE_CELL_HEIGHT = 200
DUPLICATE_HEADER_HEIGHT = 58


def render_gallery_thumbnails(pic_files: list[Path]) -> bytes:
    """按图片 ID 排序并渲染画廊缩略图。"""
    thumbnails: list[tuple[int, Image.Image]] = []
    for pic_file in sorted(pic_files, key=lambda path: int(path.stem)):
        try:
            thumbnail = _load_thumbnail(pic_file, THUMBNAIL_SIZE)
            thumbnails.append((int(pic_file.stem), thumbnail))
        except OSError as e:
            logger.warning(f"无法读取画廊图片 {pic_file}，已跳过：{e}")

    if not thumbnails:
        return b""

    columns = min(GALLERY_COLUMNS, len(thumbnails))
    rows = (len(thumbnails) + columns - 1) // columns
    cell_width = THUMBNAIL_SIZE[0]
    cell_height = THUMBNAIL_SIZE[1] + LABEL_HEIGHT
    canvas_width = GALLERY_PADDING * 2 + columns * cell_width + (columns - 1) * GALLERY_GAP
    canvas_height = GALLERY_PADDING * 2 + rows * cell_height + (rows - 1) * GALLERY_GAP
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=24)

    for index, (pic_id, thumbnail) in enumerate(thumbnails):
        row, column = divmod(index, columns)
        cell_x = GALLERY_PADDING + column * (cell_width + GALLERY_GAP)
        cell_y = GALLERY_PADDING + row * (cell_height + GALLERY_GAP)
        image_x = cell_x + (cell_width - thumbnail.width) // 2
        image_y = cell_y + (THUMBNAIL_SIZE[1] - thumbnail.height) // 2
        canvas.paste(thumbnail, (image_x, image_y), thumbnail)

        label = str(pic_id)
        label_box = draw.textbbox((0, 0), label, font=font)
        label_width = label_box[2] - label_box[0]
        label_x = cell_x + (cell_width - label_width) // 2
        label_y = cell_y + THUMBNAIL_SIZE[1] + 4
        draw.text((label_x, label_y), label, fill="black", font=font)

    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def get_gallery_overview_items() -> list[GalleryOverviewItem]:
    """Collect cover and metadata for every gallery in index order."""
    items: list[GalleryOverviewItem] = []
    for name, aliases in gallery_name_data.instance.name_to_aliases.items():
        gallery_dir = cfg.data_dir_path / name
        if not gallery_dir.is_dir():
            logger.warning(f"画廊索引中存在画廊名称 {name}，但对应的目录不存在：{gallery_dir}")
            items.append(GalleryOverviewItem(name, list(aliases), 0, None))
            continue

        picture_files = [path for path in gallery_dir.iterdir() if path.is_file()]
        id_picture_files = [path for path in picture_files if path.stem.isdigit()]
        cover_path = (
            min(id_picture_files, key=lambda path: int(path.stem)) if id_picture_files else None
        )
        items.append(
            GalleryOverviewItem(
                name=name,
                aliases=list(aliases),
                picture_count=len(picture_files),
                cover_path=cover_path,
            )
        )
    return items


def render_gallery_overview() -> bytes:
    """Render all gallery covers and metadata as an image."""
    items = get_gallery_overview_items()
    if not items:
        return b""

    title_font = _load_font(25, bold=True)
    name_font = _load_font(20, bold=True)
    detail_font = _load_font(15)
    columns = min(OVERVIEW_COLUMNS, len(items))
    text_width = OVERVIEW_COVER_SIZE[0]

    measure_canvas = Image.new("RGB", (1, 1))
    measure_draw = ImageDraw.Draw(measure_canvas)
    prepared_items: list[tuple[GalleryOverviewItem, list[str], list[str], int]] = []
    for item in items:
        name_lines = _wrap_text(measure_draw, item.name, name_font, text_width)
        aliases = "、".join(item.aliases) if item.aliases else "无"
        alias_lines = _wrap_text(
            measure_draw,
            f"别名：{aliases}",
            detail_font,
            text_width,
        )
        cell_height = (
            OVERVIEW_COVER_SIZE[1]
            + OVERVIEW_TEXT_GAP
            + len(name_lines) * 27
            + 22
            + len(alias_lines) * 21
            + 8
        )
        prepared_items.append((item, name_lines, alias_lines, cell_height))

    row_heights = [
        max(prepared[3] for prepared in prepared_items[index : index + columns])
        for index in range(0, len(prepared_items), columns)
    ]
    canvas_width = GALLERY_PADDING * 2 + columns * OVERVIEW_CELL_WIDTH + (columns - 1) * GALLERY_GAP
    canvas_height = (
        OVERVIEW_HEADER_HEIGHT
        + sum(row_heights)
        + (len(row_heights) - 1) * GALLERY_GAP
        + GALLERY_PADDING
    )
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)
    draw.text(
        (GALLERY_PADDING, GALLERY_PADDING),
        f"画廊一览  共 {len(items)} 个",
        fill="#202124",
        font=title_font,
    )

    row_y = OVERVIEW_HEADER_HEIGHT
    for index, (item, name_lines, alias_lines, _) in enumerate(prepared_items):
        row, column = divmod(index, columns)
        if column == 0 and row > 0:
            row_y += row_heights[row - 1] + GALLERY_GAP
        cell_x = GALLERY_PADDING + column * (OVERVIEW_CELL_WIDTH + GALLERY_GAP)
        cover_x = cell_x + (OVERVIEW_CELL_WIDTH - OVERVIEW_COVER_SIZE[0]) // 2
        _paste_gallery_cover(canvas, draw, item.cover_path, cover_x, row_y, detail_font)

        text_x = cover_x
        text_y = row_y + OVERVIEW_COVER_SIZE[1] + OVERVIEW_TEXT_GAP
        for line in name_lines:
            draw.text((text_x, text_y), line, fill="#202124", font=name_font)
            text_y += 27
        draw.text(
            (text_x, text_y),
            f"图片：{item.picture_count} 张",
            fill="#5f6368",
            font=detail_font,
        )
        text_y += 22
        for line in alias_lines:
            draw.text((text_x, text_y), line, fill="#5f6368", font=detail_font)
            text_y += 21

    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def _paste_gallery_cover(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    cover_path: Path | None,
    x: int,
    y: int,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> None:
    background = Image.new("RGB", OVERVIEW_COVER_SIZE, "#f1f3f4")
    canvas.paste(background, (x, y))
    if cover_path is not None:
        try:
            thumbnail = _load_thumbnail(cover_path, OVERVIEW_COVER_SIZE)
        except OSError as e:
            logger.warning(f"无法读取画廊封面 {cover_path}，将显示占位图：{e}")
        else:
            image_x = x + (OVERVIEW_COVER_SIZE[0] - thumbnail.width) // 2
            image_y = y + (OVERVIEW_COVER_SIZE[1] - thumbnail.height) // 2
            canvas.paste(thumbnail, (image_x, image_y), thumbnail)
            return

    placeholder = "暂无图片"
    box = draw.textbbox((0, 0), placeholder, font=font)
    text_width = box[2] - box[0]
    text_height = box[3] - box[1]
    draw.text(
        (
            x + (OVERVIEW_COVER_SIZE[0] - text_width) // 2,
            y + (OVERVIEW_COVER_SIZE[1] - text_height) // 2 - box[1],
        ),
        placeholder,
        fill="#80868b",
        font=font,
    )


def _wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    lines: list[str] = []
    current_line = ""
    for character in text:
        candidate = current_line + character
        if current_line and draw.textlength(candidate, font=font) > max_width:
            lines.append(current_line)
            current_line = character
        else:
            current_line = candidate
    if current_line or not lines:
        lines.append(current_line)
    return lines


def _load_font(
    size: int,
    *,
    bold: bool = False,
) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_names = (
        (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "NotoSansCJK-Bold.ttc",
        )
        if bold
        else (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "NotoSansCJK-Regular.ttc",
        )
    )
    for font_name in font_names:
        try:
            return ImageFont.truetype(font_name, size=size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _load_thumbnail(image_path: Path, size: tuple[int, int]) -> Image.Image:
    with Image.open(image_path) as source:
        source = ImageOps.exif_transpose(source)
        thumbnail = ImageOps.contain(
            source.convert("RGBA"),
            size,
            Image.Resampling.LANCZOS,
        )
        return thumbnail.copy()


def render_duplicate_comparisons(duplicates: list[DuplicatePicture]) -> bytes:
    """Render candidate and existing pictures side by side."""
    if not duplicates:
        return b""

    columns = min(DUPLICATE_COLUMNS, len(duplicates))
    rows = (len(duplicates) + columns - 1) // columns
    canvas_width = GALLERY_PADDING * 2 + columns * DUPLICATE_CELL_WIDTH
    canvas_height = DUPLICATE_HEADER_HEIGHT + GALLERY_PADDING + rows * DUPLICATE_CELL_HEIGHT
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)
    title_font = ImageFont.load_default(size=24)
    label_font = ImageFont.load_default(size=17)
    detail_font = ImageFont.load_default(size=14)
    draw.text(
        (GALLERY_PADDING, GALLERY_PADDING),
        f"Duplicate pictures: {len(duplicates)}",
        fill="#202124",
        font=title_font,
    )

    for index, duplicate in enumerate(duplicates):
        row, column = divmod(index, columns)
        cell_x = GALLERY_PADDING + column * DUPLICATE_CELL_WIDTH
        cell_y = DUPLICATE_HEADER_HEIGHT + row * DUPLICATE_CELL_HEIGHT
        if column:
            draw.line(
                (cell_x, cell_y, cell_x, cell_y + DUPLICATE_CELL_HEIGHT - 12),
                fill="#dadce0",
                width=1,
            )

        left_x = cell_x + 8
        right_x = cell_x + 220
        image_y = cell_y + 28
        _paste_comparison_thumbnail(canvas, duplicate.candidate_path, left_x, image_y)
        _paste_comparison_thumbnail(canvas, duplicate.existing_path, right_x, image_y)
        draw.text(
            (left_x, cell_y + 2),
            f"New #{duplicate.candidate_index}",
            fill="#202124",
            font=label_font,
        )
        existing_id = duplicate.existing_path.stem
        draw.text(
            (right_x, cell_y + 2),
            f"Existing #{existing_id}",
            fill="#202124",
            font=label_font,
        )
        draw.text(
            (cell_x + 198, image_y + 54),
            "=",
            fill="#d93025",
            font=title_font,
        )
        draw.text(
            (left_x, image_y + DUPLICATE_THUMBNAIL_SIZE[1] + 5),
            duplicate.reason,
            fill="#5f6368",
            font=detail_font,
        )

    output = BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()


def _paste_comparison_thumbnail(
    canvas: Image.Image,
    image_path: Path,
    x: int,
    y: int,
) -> None:
    thumbnail = _load_thumbnail(image_path, DUPLICATE_THUMBNAIL_SIZE)
    background = Image.new("RGB", DUPLICATE_THUMBNAIL_SIZE, "#f1f3f4")
    background_x = x
    background_y = y
    canvas.paste(background, (background_x, background_y))
    image_x = x + (DUPLICATE_THUMBNAIL_SIZE[0] - thumbnail.width) // 2
    image_y = y + (DUPLICATE_THUMBNAIL_SIZE[1] - thumbnail.height) // 2
    canvas.paste(thumbnail, (image_x, image_y), thumbnail)
