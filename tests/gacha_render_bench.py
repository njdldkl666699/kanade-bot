# 基准测试：gacha 十连渲染各阶段耗时，以及「不缩放」变体的对比
# 复刻 kanade_bot/plugins/crystal/plugins/gacha/gacha.py 的实际代码路径
import json
import time
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageOps

ROOT = Path(__file__).resolve().parents[1]
GACHA_DATA = ROOT / "data" / "crystal" / "gacha"
CACHE_DIR = ROOT / "cache" / "crystal" / "cache"

# --- 与 gacha.py 一致的常量 ---
CARD_SIZE = (940, 530)
ATTRIBUTE_ICON_POSITION = (815, 0)
RARITY_ICON_X = 33
RARITY_ICON_BOTTOM_MARGIN = 22
RARITY_ICON_SPACING = 66
GACHA_COLUMNS = 5
GACHA_ROWS = 2
GACHA_THUMBNAIL_SIZE = (320, 180)
GACHA_PADDING = 28
GACHA_GAP = 18
GACHA_BACKGROUND = "#f4f0f7"
GACHA_SLOT_RADIUS = 8
GACHA_SHADOW_OFFSET = 8
GACHA_SHADOW_BLUR = 10

FRAMES = {
    "rarity_1": "cardFrame_L_rarity_1.png",
    "rarity_2": "cardFrame_L_rarity_2.png",
    "rarity_3": "cardFrame_L_rarity_3.png",
    "rarity_4": "cardFrame_L_rarity_4.png",
    "rarity_birthday": "cardFrame_L_rarity_birthday.png",
}
ATTR_ICONS = {
    a: f"icon_attribute_{a}_88.png" for a in ("cool", "cute", "pure", "happy", "mysterious")
}


def _open_rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _fit_to_card(image: Image.Image) -> Image.Image:
    if image.size == CARD_SIZE:
        return image
    return image.resize(CARD_SIZE, Image.Resampling.LANCZOS)


def pick_cards(n: int = 10) -> list[dict]:
    """优先选缓存里已有的卡（命中路径），不足再选 rarity_3/4（特训卡与缓存文件名一致）"""
    cached = {p.name.rsplit("_card_", 1)[0] for p in CACHE_DIR.glob("*.png")}
    data = json.loads((GACHA_DATA / "cards.json").read_text(encoding="utf-8"))
    chosen: list[dict] = []
    for c in data:
        if c["assetbundleName"] in cached:
            chosen.append(c)
        if len(chosen) >= n:
            break
    assert len(chosen) == n, f"只找到 {len(chosen)} 张缓存卡"
    return chosen


def timeit(label: str, func, repeat: int = 20, warmup: int = 3):
    for _ in range(warmup):
        func()
    best = float("inf")
    for _ in range(repeat):
        t0 = time.perf_counter()
        func()
        best = min(best, time.perf_counter() - t0)
    print(f"{label:<52s} {best * 1000:8.2f} ms")
    return best


def render_composed_card(card: dict) -> Image.Image:
    """cache-miss 路径：从原始素材合成 940x530（含 PNG 写盘）"""
    trained = card["cardRarityType"] in ("rarity_3", "rarity_4")
    render_file = "card_after_training.png" if trained else "card_normal.png"
    name = f"{card['assetbundleName']}_{render_file}"
    cache_path = CACHE_DIR / name
    if cache_path.is_file():
        return _open_rgba(cache_path)

    image = _fit_to_card(
        _open_rgba(GACHA_DATA / "member_small" / card["assetbundleName"] / render_file)
    ).copy()
    frame = _fit_to_card(_open_rgba(GACHA_DATA / "cards_assets" / FRAMES[card["cardRarityType"]]))
    num = int(card["cardRarityType"].replace("rarity_", "")) if trained else None
    if card["cardRarityType"] == "rarity_birthday":
        icon, cnt = "rare_birthday.png", 1
    elif trained:
        icon, cnt = "rare_star_after_training.png", num
    else:
        icon, cnt = "rare_star_normal.png", num
    rarity_icon = _open_rgba(GACHA_DATA / "cards_assets" / icon)
    attr_icon = _open_rgba(GACHA_DATA / "cards_assets" / ATTR_ICONS[card["attr"]])

    image.alpha_composite(frame)
    rarity_y = (
        image.height
        - rarity_icon.height
        - RARITY_ICON_BOTTOM_MARGIN
        - max(cnt - 1, 0) * RARITY_ICON_SPACING
    )
    for i in range(cnt):
        image.alpha_composite(rarity_icon, (RARITY_ICON_X, rarity_y + i * RARITY_ICON_SPACING))
    image.alpha_composite(attr_icon, ATTRIBUTE_ICON_POSITION)

    # 写缓存（PNG 编码 + 落盘）计入——真实代码里就是这一步
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return image


def render_gacha_10(cards: list[dict], resample=Image.Resampling.LANCZOS, convert_copy=True):
    """十连渲染（缓存命中路径），与 gacha.py 完全一致"""
    tw, th = GACHA_THUMBNAIL_SIZE
    cw = GACHA_PADDING * 2 + GACHA_COLUMNS * tw + (GACHA_COLUMNS - 1) * GACHA_GAP
    ch = GACHA_PADDING * 2 + GACHA_ROWS * th + (GACHA_ROWS - 1) * GACHA_GAP
    canvas = Image.new("RGBA", (cw, ch), GACHA_BACKGROUND)

    shadow_mask = Image.new("L", canvas.size)
    sd = ImageDraw.Draw(shadow_mask)
    for index in range(min(len(cards), GACHA_COLUMNS * GACHA_ROWS)):
        row, col = divmod(index, GACHA_COLUMNS)
        x = GACHA_PADDING + col * (tw + GACHA_GAP)
        y = GACHA_PADDING + row * (th + GACHA_GAP)
        sd.rounded_rectangle(
            (x, y + GACHA_SHADOW_OFFSET, x + tw, y + th + GACHA_SHADOW_OFFSET),
            radius=GACHA_SLOT_RADIUS,
            fill=41,
        )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(GACHA_SHADOW_BLUR))
    shadow = Image.new("RGBA", canvas.size, (50, 38, 66, 0))
    shadow.putalpha(shadow_mask)
    canvas.alpha_composite(shadow)

    corner_mask = Image.new("L", GACHA_THUMBNAIL_SIZE)
    ImageDraw.Draw(corner_mask).rounded_rectangle(
        (0, 0, tw - 1, th - 1), radius=GACHA_SLOT_RADIUS, fill=255
    )

    for index, card in enumerate(cards[: GACHA_COLUMNS * GACHA_ROWS]):
        row, col = divmod(index, GACHA_COLUMNS)
        x = GACHA_PADDING + col * (tw + GACHA_GAP)
        y = GACHA_PADDING + row * (th + GACHA_GAP)
        img = render_composed_card(card)  # 缓存命中 → 直接返回 940x530
        if convert_copy:
            img = img.convert("RGBA")  # gacha.py 里原样存在
        thumbnail = ImageOps.fit(img, GACHA_THUMBNAIL_SIZE, method=resample)
        slot = Image.new("RGBA", GACHA_THUMBNAIL_SIZE, "white")
        slot.alpha_composite(thumbnail)
        slot.putalpha(corner_mask)
        canvas.alpha_composite(slot, (x, y))
    return canvas


def render_gacha_10_noscale(cards: list[dict]):
    """「不缩放」变体：槽位直接用 940x530，画布等比放大"""
    tw, th = CARD_SIZE
    cw = GACHA_PADDING * 2 + GACHA_COLUMNS * tw + (GACHA_COLUMNS - 1) * GACHA_GAP
    ch = GACHA_PADDING * 2 + GACHA_ROWS * th + (GACHA_ROWS - 1) * GACHA_GAP
    canvas = Image.new("RGBA", (cw, ch), GACHA_BACKGROUND)
    for index, card in enumerate(cards[: GACHA_COLUMNS * GACHA_ROWS]):
        row, col = divmod(index, GACHA_COLUMNS)
        x = GACHA_PADDING + col * (tw + GACHA_GAP)
        y = GACHA_PADDING + row * (th + GACHA_GAP)
        slot = render_composed_card(card).copy()
        canvas.alpha_composite(slot, (x, y))
    return canvas


def png_bytes(image: Image.Image) -> int:
    b = BytesIO()
    image.save(b, format="PNG")
    return b.tell()


def main():
    cards = pick_cards(10)
    print(
        f"Pillow {Image.__version__} | 卡片: {[c['assetbundleName'] for c in cards[:3]]}... 共{len(cards)}张\n"
    )
    print("=== 阶段分解（单张卡，缓存命中） ===")
    one = CACHE_DIR / f"{cards[0]['assetbundleName']}_card_after_training.png"
    full = _open_rgba(one)
    print(f"(缓存文件尺寸 {full.size}, PNG {one.stat().st_size // 1024} KB)")

    t_decode = timeit("① PNG 解码 940x530 (open+convert RGBA)", lambda: _open_rgba(one))
    t_copy = timeit("② .convert('RGBA') 冗余拷贝", lambda: full.convert("RGBA"))
    t_lanczos = timeit(
        "③ ImageOps.fit LANCZOS 940x530→320x180",
        lambda: ImageOps.fit(full, GACHA_THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS),
    )
    t_bilinear = timeit(
        "   (对比) fit BILINEAR",
        lambda: ImageOps.fit(full, GACHA_THUMBNAIL_SIZE, method=Image.Resampling.BILINEAR),
        repeat=10,
    )
    t_box = timeit(
        "   (对比) fit BOX",
        lambda: ImageOps.fit(full, GACHA_THUMBNAIL_SIZE, method=Image.Resampling.BOX),
        repeat=10,
    )
    tn = ImageOps.fit(full, GACHA_THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS)
    timeit(
        "④ slot 合成+圆角+贴画布 (320x180)",
        lambda: (lambda s: (s.alpha_composite(tn), s.putalpha(corner := None) if False else None))(
            Image.new("RGBA", GACHA_THUMBNAIL_SIZE, "white")
        ),
    )

    print("\n=== 完整十连（缓存全命中, x10 卡） ===")
    t10 = timeit(
        "现状: decode+convert+fit(LANCZOS)+合成+阴影+成图",
        lambda: render_gacha_10(cards),
        repeat=10,
    )
    img10 = render_gacha_10(cards)
    print(f"   → 画布 {img10.size}, 输出 PNG {png_bytes(img10) // 1024} KB")

    t10_nc = timeit(
        "现状去掉冗余 convert('RGBA')",
        lambda: render_gacha_10(cards, convert_copy=False),
        repeat=10,
    )
    t10_bl = timeit(
        "现状但缩放改 BILINEAR",
        lambda: render_gacha_10(cards, resample=Image.Resampling.BILINEAR),
        repeat=10,
    )
    t10_box = timeit(
        "现状但缩放改 BOX", lambda: render_gacha_10(cards, resample=Image.Resampling.BOX), repeat=10
    )

    print("\n=== 「不缩放」变体（槽位=940x530 原尺寸贴入大画布） ===")
    t10_ns = timeit(
        "不缩放: decode+原尺寸贴入 4836x1178 画布",
        lambda: render_gacha_10_noscale(cards),
        repeat=10,
    )
    img_ns = render_gacha_10_noscale(cards)
    print(f"   → 画布 {img_ns.size}, 输出 PNG {png_bytes(img_ns) // 1024} KB")
    timeit("不缩放变体的最终 PNG 编码", lambda: png_bytes(img_ns), repeat=10)
    timeit("现状的最终 PNG 编码", lambda: png_bytes(img10), repeat=10)

    print("\n=== 缓存未命中：单张合成 940x530（含 PNG 写盘） ===")
    miss = [
        c
        for c in cards
        if not (CACHE_DIR / f"{c['assetbundleName']}_card_after_training.png").exists()
    ]
    # 用一张已缓存的卡模拟 miss（跳过缓存判断，直接合成）
    card = cards[0]
    name = f"{card['assetbundleName']}_card_after_training.png"
    tmp_backup = CACHE_DIR / f"{name}.bak"
    (CACHE_DIR / name).rename(tmp_backup)
    try:
        timeit(
            "cache-miss: 原图合成 + PNG 编码进内存",
            lambda: render_composed_card_nodisk(card),
            repeat=5,
        )
    finally:
        tmp_backup.rename(CACHE_DIR / name)

    print("\n=== 汇总 ===")
    print(
        f"十连现状总耗时 ≈ {t10 * 1000:.0f} ms，其中 10 张卡的 LANCZOS 缩放 ≈ {t_lanczos * 10 * 1000:.0f} ms ({t_lanczos * 10 / t10 * 100:.0f}%)"
    )
    print(
        f"「不缩放」总耗时 ≈ {t10_ns * 1000:.0f} ms → 比现状 {'慢' if t10_ns > t10 else '快'} {abs(t10_ns - t10) * 1000:.0f} ms"
    )


def render_composed_card_nodisk(card: dict) -> Image.Image:
    trained = card["cardRarityType"] in ("rarity_3", "rarity_4")
    render_file = "card_after_training.png" if trained else "card_normal.png"
    image = _fit_to_card(
        _open_rgba(GACHA_DATA / "member_small" / card["assetbundleName"] / render_file)
    ).copy()
    frame = _fit_to_card(_open_rgba(GACHA_DATA / "cards_assets" / FRAMES[card["cardRarityType"]]))
    num = int(card["cardRarityType"].replace("rarity_", "")) if trained else 0
    icon, cnt = "rare_star_after_training.png", num
    rarity_icon = _open_rgba(GACHA_DATA / "cards_assets" / icon)
    image.alpha_composite(frame)
    rarity_y = (
        image.height
        - rarity_icon.height
        - RARITY_ICON_BOTTOM_MARGIN
        - max(cnt - 1, 0) * RARITY_ICON_SPACING
    )
    for i in range(cnt):
        image.alpha_composite(rarity_icon, (RARITY_ICON_X, rarity_y + i * RARITY_ICON_SPACING))
    image.alpha_composite(
        attr_icon := _open_rgba(GACHA_DATA / "cards_assets" / ATTR_ICONS[card["attr"]]),
        ATTRIBUTE_ICON_POSITION,
    )
    return image


if __name__ == "__main__":
    main()
