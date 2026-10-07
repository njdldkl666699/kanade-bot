# 验证十连缩略图缓存（与 gacha.py 中 render_composed_card_thumbnail 逻辑一致）
# 1. 缓存文件生成在 cache/crystal/cache/ 下，文件名带尺寸后缀
# 2. 缓存命中返回的图像与「直接对全尺寸图 ImageOps.fit」逐像素一致
# 3. 十连渲染提速效果
import json
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

ROOT = Path(__file__).resolve().parents[1]
GACHA_DATA = ROOT / "data" / "crystal" / "gacha"
CACHE_DIR = ROOT / "cache" / "crystal" / "cache"

CARD_SIZE = (940, 530)
GACHA_THUMBNAIL_SIZE = (320, 180)


def _open_rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def render_composed_card(bundle: str) -> Image.Image:
    """缓存命中路径：直接解码全尺寸渲染图"""
    return _open_rgba(CACHE_DIR / f"{bundle}_card_after_training.png")


def render_composed_card_thumbnail(bundle: str) -> Image.Image:
    """与 gacha.py 新逻辑完全一致"""
    width, height = GACHA_THUMBNAIL_SIZE
    cache_rendered_file = f"{bundle}_card_after_training_{width}x{height}.png"
    cache_file_path = CACHE_DIR / cache_rendered_file
    if cache_file_path.is_file():
        return _open_rgba(cache_file_path)

    thumbnail = ImageOps.fit(
        render_composed_card(bundle),
        GACHA_THUMBNAIL_SIZE,
        method=Image.Resampling.LANCZOS,
    )
    cache_file_path.parent.mkdir(parents=True, exist_ok=True)
    thumbnail.save(cache_file_path, format="PNG")
    return thumbnail


def pick_bundles(n: int = 10) -> list[str]:
    cached = sorted(p.name.split("_card_")[0] for p in CACHE_DIR.glob("*_card_after_training.png"))
    return cached[:n]


def main():
    bundles = pick_bundles(10)
    print(f"测试卡片: {bundles[:3]} ... 共 {len(bundles)} 张\n")

    bundle = bundles[0]
    thumb_path = CACHE_DIR / f"{bundle}_card_after_training_320x180.png"
    if thumb_path.exists():
        thumb_path.unlink()  # 保证从 miss 开始

    # ① miss 路径：生成缓存
    t0 = time.perf_counter()
    thumb_miss = render_composed_card_thumbnail(bundle)
    t_miss = time.perf_counter() - t0
    assert thumb_path.is_file(), "缩略图缓存文件未生成"
    assert thumb_path.parent == CACHE_DIR, "缩略图未落在 cache/crystal/cache/"
    print(
        f"① miss: 生成 {thumb_path.name} ({thumb_path.stat().st_size // 1024} KB), 耗时 {t_miss * 1000:.1f} ms"
    )

    # ② 命中路径：与直接 ImageOps.fit 全尺寸图逐像素对比
    expected = ImageOps.fit(
        render_composed_card(bundle).convert("RGBA"),
        GACHA_THUMBNAIL_SIZE,
        method=Image.Resampling.LANCZOS,
    )
    thumb_hit = render_composed_card_thumbnail(bundle)
    assert thumb_hit.size == GACHA_THUMBNAIL_SIZE
    assert thumb_hit.mode == "RGBA"
    diff = ImageChops.difference(expected, thumb_hit).getbbox()
    assert diff is None, f"缓存命中结果与直接缩放不一致: {diff}"
    print("② hit:  与直接 ImageOps.fit(LANCZOS) 逐像素一致 ✓")

    # ③ 十连提速：旧路径（每次解码全尺寸+fit）vs 新路径（解码缩略图）
    for b in bundles:
        render_composed_card_thumbnail(b)  # 预热所有缩略图缓存

    def old_way():
        return [
            ImageOps.fit(
                render_composed_card(b).convert("RGBA"),
                GACHA_THUMBNAIL_SIZE,
                method=Image.Resampling.LANCZOS,
            )
            for b in bundles
        ]

    def new_way():
        return [render_composed_card_thumbnail(b) for b in bundles]

    for name, fn in (("旧(全尺寸解码+LANCZOS)", old_way), ("新(缩略图缓存命中)", new_way)):
        fn()  # warmup
        best = min(
            (lambda t0=t0: (t0 := time.perf_counter(), fn(), time.perf_counter() - t0)[2])()
            for _ in range(20)
        )
        print(f"③ {name}: 10 张 {best * 1000:.1f} ms")

    print("\n全部通过 ✓")


if __name__ == "__main__":
    main()
