import shutil
from io import BytesIO
from pathlib import Path

from nonebot import logger
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .config import cfg, gallery_name_data


def get_gallery_name(name_or_alias: str) -> str | None:
    """根据名称或别名获取画廊名称"""
    v = gallery_name_data.instance
    if name_or_alias in v.name_to_aliases:
        return name_or_alias
    return v.alias_to_name.get(name_or_alias)


def get_picture_by_id(pic_id: int) -> Path | None:
    """根据图片id获取图片文件路径"""
    pic_files = list(cfg.data_dir_path.glob(f"{pic_id}.*"))
    if not pic_files:
        return None
    return pic_files[0]


def save_pictures(name: str, pic_paths: list[Path]):
    """保存图片文件到画廊目录"""
    gallery_dir = cfg.data_dir_path / name
    gallery_dir.mkdir(parents=True, exist_ok=True)

    v = gallery_name_data.instance
    for pic_path in pic_paths:
        # 生成图片文件名
        pic_id = v.iota + 1
        v.iota = pic_id
        suffix = pic_path.suffix
        new_pic_path = gallery_dir / f"{pic_id}{suffix}"
        shutil.copy(pic_path, new_pic_path)

    gallery_name_data.save_to_file()


GALLERY_COLUMNS = 4
THUMBNAIL_SIZE = (200, 150)
GALLERY_PADDING = 16
GALLERY_GAP = 12
LABEL_HEIGHT = 36


def render_gallery_thumbnails(pic_files: list[Path]) -> bytes:
    """按图片 ID 排序并渲染画廊缩略图。"""
    thumbnails: list[tuple[int, Image.Image]] = []
    for pic_file in sorted(pic_files, key=lambda path: int(path.stem)):
        try:
            with Image.open(pic_file) as source:
                source = ImageOps.exif_transpose(source)
                thumbnail = ImageOps.contain(
                    source.convert("RGBA"),
                    THUMBNAIL_SIZE,
                    Image.Resampling.LANCZOS,
                )
                thumbnails.append((int(pic_file.stem), thumbnail.copy()))
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
