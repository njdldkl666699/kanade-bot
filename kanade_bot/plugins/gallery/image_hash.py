import hashlib
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageOps

HASH_SIZE = 8
PHASH_IMAGE_SIZE = HASH_SIZE * 4


@dataclass(frozen=True)
class ImageHashes:
    file_hash: str
    dhash: int
    phash: int
    ahash: int | None


def calculate_image_hashes(image_path: Path) -> ImageHashes:
    """Calculate the exact and perceptual hashes used for duplicate checks."""
    digest = hashlib.md5(usedforsecurity=False)
    with image_path.open("rb") as image_file:
        for chunk in iter(lambda: image_file.read(1024 * 1024), b""):
            digest.update(chunk)

    with Image.open(image_path) as source:
        grayscale = ImageOps.exif_transpose(source).convert("L")
        return ImageHashes(
            file_hash=digest.hexdigest(),
            dhash=_calculate_dhash(grayscale),
            phash=_calculate_phash(grayscale),
            ahash=_calculate_ahash(grayscale),
        )


def hamming_distance(first: int, second: int) -> int:
    return (first ^ second).bit_count()


def perceptual_distances(
    first: ImageHashes,
    second: ImageHashes,
) -> tuple[int, int, int] | None:
    """Return distances when at least two reference thresholds are met."""
    if first.ahash is None or second.ahash is None:
        return None

    distances = (
        hamming_distance(first.dhash, second.dhash),
        hamming_distance(first.phash, second.phash),
        hamming_distance(first.ahash, second.ahash),
    )
    if sum(distance < threshold for distance, threshold in zip(distances, (8, 2, 2))) < 2:
        return None
    return distances


def _calculate_dhash(image: Image.Image) -> int:
    pixels = list(
        image.resize(
            (HASH_SIZE + 1, HASH_SIZE),
            Image.Resampling.LANCZOS,
        ).get_flattened_data()
    )
    bits = (
        pixels[row * (HASH_SIZE + 1) + column + 1] > pixels[row * (HASH_SIZE + 1) + column]
        for row in range(HASH_SIZE)
        for column in range(HASH_SIZE)
    )
    return _bits_to_int(bits)


def _calculate_ahash(image: Image.Image) -> int | None:
    pixels = list(
        image.resize(
            (HASH_SIZE, HASH_SIZE),
            Image.Resampling.LANCZOS,
        ).get_flattened_data()
    )
    average = sum(pixels) / len(pixels)
    variance = sum((pixel - average) ** 2 for pixel in pixels) / len(pixels)
    if math.sqrt(variance) < 3.0:
        return None
    return _bits_to_int(pixel > average for pixel in pixels)


def _calculate_phash(image: Image.Image) -> int:
    pixels = list(
        image.resize(
            (PHASH_IMAGE_SIZE, PHASH_IMAGE_SIZE),
            Image.Resampling.LANCZOS,
        ).get_flattened_data()
    )
    cosine_table = _cosine_table()
    # 二维 DCT 可分离为两次一维变换，避免为每个系数重复完整遍历图像。
    horizontal = [
        [
            sum(
                pixels[y * PHASH_IMAGE_SIZE + x] * cosine_table[frequency][x]
                for x in range(PHASH_IMAGE_SIZE)
            )
            for frequency in range(HASH_SIZE)
        ]
        for y in range(PHASH_IMAGE_SIZE)
    ]
    coefficients = [
        sum(
            horizontal[y][horizontal_frequency] * cosine_table[vertical_frequency][y]
            for y in range(PHASH_IMAGE_SIZE)
        )
        for vertical_frequency in range(HASH_SIZE)
        for horizontal_frequency in range(HASH_SIZE)
    ]

    # The reference implementation excludes the DC coefficient and emits complete
    # hexadecimal nibbles, resulting in a 60-bit pHash.
    frequency_coefficients = coefficients[1:]
    ordered = sorted(frequency_coefficients)
    median = ordered[len(ordered) // 2]
    return _bits_to_int(coefficient > median for coefficient in frequency_coefficients[:60])


@lru_cache(maxsize=1)
def _cosine_table() -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(
            math.cos(math.pi * frequency * position / PHASH_IMAGE_SIZE)
            for position in range(PHASH_IMAGE_SIZE)
        )
        for frequency in range(HASH_SIZE)
    )


def _bits_to_int(bits) -> int:
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return value
