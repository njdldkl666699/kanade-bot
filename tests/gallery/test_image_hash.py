import importlib.util
from pathlib import Path

from PIL import Image, ImageOps

MODULE_PATH = Path(__file__).parents[2] / "kanade_bot/plugins/gallery/image_hash.py"
SPEC = importlib.util.spec_from_file_location("gallery_image_hash_under_test", MODULE_PATH)
image_hash = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(image_hash)


def _reference_phash(image: Image.Image) -> int:
    pixels = list(
        image.resize(
            (image_hash.PHASH_IMAGE_SIZE, image_hash.PHASH_IMAGE_SIZE),
            Image.Resampling.LANCZOS,
        ).get_flattened_data()
    )
    cosine_table = image_hash._cosine_table()
    coefficients = []
    for vertical_frequency in range(image_hash.HASH_SIZE):
        vertical_cosines = cosine_table[vertical_frequency]
        for horizontal_frequency in range(image_hash.HASH_SIZE):
            horizontal_cosines = cosine_table[horizontal_frequency]
            coefficients.append(
                sum(
                    pixels[y * image_hash.PHASH_IMAGE_SIZE + x]
                    * horizontal_cosines[x]
                    * vertical_cosines[y]
                    for y in range(image_hash.PHASH_IMAGE_SIZE)
                    for x in range(image_hash.PHASH_IMAGE_SIZE)
                )
            )
    frequency_coefficients = coefficients[1:]
    median = sorted(frequency_coefficients)[len(frequency_coefficients) // 2]
    return image_hash._bits_to_int(
        value > median for value in frequency_coefficients[:60]
    )


def test_separable_phash_matches_reference_implementation():
    image = Image.new("RGB", (83, 59), "white")
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            pixels[x, y] = ((x * 17) % 256, (y * 29) % 256, ((x + y) * 11) % 256)
    grayscale = ImageOps.exif_transpose(image).convert("L")
    assert image_hash._calculate_phash(grayscale) == _reference_phash(grayscale)
