"""图片入库/发送前的压缩行为"""

from __future__ import annotations

import io
import unittest

from PIL import Image

from kanade_bot.utils.image import MAX_IMAGE_EDGE, compress_image


class ImageCompressTest(unittest.TestCase):
    def test_large_noisy_image_is_downscaled_to_webp(self):
        image = Image.effect_noise((2400, 1600), 100).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()

        compressed, media_type = compress_image(raw, "image/png")

        self.assertEqual(media_type, "image/webp")
        self.assertLess(len(compressed), len(raw))
        with Image.open(io.BytesIO(compressed)) as result:
            self.assertEqual(result.format, "WEBP")
            self.assertLessEqual(max(result.size), MAX_IMAGE_EDGE)

    def test_no_resize_when_max_edge_is_none(self):
        image = Image.effect_noise((2400, 1600), 100).convert("RGB")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()

        compressed, media_type = compress_image(raw, "image/png", max_edge=None, min_size=0)

        self.assertEqual(media_type, "image/webp")
        self.assertLess(len(compressed), len(raw))
        with Image.open(io.BytesIO(compressed)) as result:
            self.assertEqual(result.size, (2400, 1600))

    def test_small_image_is_unchanged(self):
        image = Image.new("RGB", (64, 64), (120, 30, 200))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        raw = buffer.getvalue()

        compressed, media_type = compress_image(raw, "image/png")

        self.assertEqual(compressed, raw)
        self.assertEqual(media_type, "image/png")

    def test_animated_gif_is_unchanged(self):
        frames = [Image.new("RGB", (64, 64), color) for color in ((255, 0, 0), (0, 0, 255))]
        buffer = io.BytesIO()
        frames[0].save(
            buffer,
            format="GIF",
            save_all=True,
            append_images=frames[1:],
            duration=100,
            loop=0,
        )
        raw = buffer.getvalue()

        compressed, media_type = compress_image(raw, "image/gif")

        self.assertEqual(compressed, raw)
        self.assertEqual(media_type, "image/gif")

    def test_invalid_image_falls_back_to_original(self):
        raw = b"not an image"

        compressed, media_type = compress_image(raw, "image/png")

        self.assertEqual(compressed, raw)
        self.assertEqual(media_type, "image/png")
