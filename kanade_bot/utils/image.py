from io import BytesIO

from nonebot import logger
from PIL import Image as PILImage
from PIL import ImageOps

MAX_IMAGE_EDGE = 2048
"""图片输入压缩后的长边上限（像素）"""

WEBP_QUALITY = 82
"""WebP/JPEG 默认压缩质量"""

MIN_COMPRESS_BYTES = 256 * 1024
"""小于该体积且长边不超限的图片保持原样"""


def _has_alpha(image: PILImage.Image) -> bool:
    return image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)


def _to_rgba(image: PILImage.Image) -> PILImage.Image:
    if image.mode == "RGBA":
        return image
    return image.convert("RGBA")


def _to_rgb(image: PILImage.Image) -> PILImage.Image:
    """转 RGB；透明背景填白（JPEG 不支持 alpha）"""

    if image.mode == "RGB":
        return image
    rgba = image.convert("RGBA")
    background = PILImage.new("RGB", rgba.size, (255, 255, 255))
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def _encode_webp(image: PILImage.Image, *, quality: int, lossless: bool) -> bytes:
    out = BytesIO()
    image.save(out, format="WEBP", quality=quality, lossless=lossless, method=6)
    return out.getvalue()


def _encode_jpeg(image: PILImage.Image, *, quality: int) -> bytes:
    out = BytesIO()
    _to_rgb(image).save(out, format="JPEG", quality=quality, optimize=True)
    return out.getvalue()


def compress_image(
    data: bytes,
    media_type: str | None,
    *,
    max_edge: int | None = MAX_IMAGE_EDGE,
    quality: int = WEBP_QUALITY,
    min_size: int = MIN_COMPRESS_BYTES,
    lossless: bool = False,
) -> tuple[bytes, str]:
    """压缩图片，返回 `(压缩后字节, media_type)`

    :param max_edge: 长边上限；None 表示不缩放（仅重新编码）
    :param quality: WebP/JPEG 质量
    :param min_size: 小于该体积且无需缩放时保持原样
    :param lossless: WebP 是否使用无损编码（仅静图）
    """

    fallback_type = media_type or "application/octet-stream"
    if not data or (media_type and not media_type.startswith("image/")):
        return data, fallback_type

    try:
        with PILImage.open(BytesIO(data)) as image:
            if getattr(image, "is_animated", False) and getattr(image, "n_frames", 1) > 1:
                return data, fallback_type

            need_resize = max_edge is not None and max(image.size) > max_edge
            if not need_resize and len(data) <= min_size:
                return data, fallback_type

            alpha = _has_alpha(image)
            image = ImageOps.exif_transpose(image)
            if need_resize and max_edge is not None:
                image.thumbnail((max_edge, max_edge), PILImage.Resampling.LANCZOS)

            try:
                webp = _encode_webp(
                    _to_rgba(image) if alpha else _to_rgb(image),
                    quality=quality,
                    lossless=lossless,
                )
            except Exception as e:
                logger.debug(f"WebP 编码失败，尝试 JPEG：{e}")
                webp = None
            if webp is not None and len(webp) < len(data):
                logger.debug(
                    f"图片压缩（WebP）：{len(data) / 1024:.0f}KB → {len(webp) / 1024:.0f}KB，"
                    f"{image.width}x{image.height}"
                )
                return webp, "image/webp"

            try:
                jpeg = _encode_jpeg(image, quality=quality)
            except Exception as e:
                logger.debug(f"JPEG 编码失败：{e}")
                jpeg = None
            if jpeg is not None and len(jpeg) < len(data):
                logger.debug(
                    f"图片压缩（JPEG）：{len(data) / 1024:.0f}KB → {len(jpeg) / 1024:.0f}KB，"
                    f"{image.width}x{image.height}"
                )
                return jpeg, "image/jpeg"
    except Exception as e:
        logger.warning(f"图片压缩失败，使用原图：{e}")
        return data, fallback_type

    logger.debug(f"图片压缩收益不足，保留原图（{len(data) / 1024:.0f}KB）")
    return data, fallback_type
