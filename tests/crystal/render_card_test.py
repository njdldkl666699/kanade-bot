from pathlib import Path

from PIL import Image

CARD_SIZE = (940, 530)
ATTRIBUTE_ICON_POSITION = (815, 0)
RARITY_ICON_X = 33
RARITY_ICON_BOTTOM_MARGIN = 22
RARITY_ICON_SPACING = 66


def _open_rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _fit_to_card(image: Image.Image) -> Image.Image:
    if image.size == CARD_SIZE:
        return image
    return image.resize(CARD_SIZE, Image.Resampling.LANCZOS)


def compose_card_image(
    card_path: Path,
    card_frame_path: Path,
    rarity_icon_path: Path,
    rarity_count: int,
    attribute_icon_path: Path,
) -> Image.Image:
    """Compose a single gacha card image from fixed asset paths."""
    image = _fit_to_card(_open_rgba(card_path)).copy()
    frame = _fit_to_card(_open_rgba(card_frame_path))
    rarity_icon = _open_rgba(rarity_icon_path)
    attribute_icon = _open_rgba(attribute_icon_path)

    image.alpha_composite(frame)

    rarity_y = (
        image.height
        - rarity_icon.height
        - RARITY_ICON_BOTTOM_MARGIN
        - max(rarity_count - 1, 0) * RARITY_ICON_SPACING
    )
    for index in range(rarity_count):
        image.alpha_composite(
            rarity_icon,
            (RARITY_ICON_X, rarity_y + index * RARITY_ICON_SPACING),
        )

    image.alpha_composite(attribute_icon, ATTRIBUTE_ICON_POSITION)
    return image


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = ROOT / "render_card_output.png"

CARD_PATH = ROOT / "data" / "crystal" / "member_small" / "res003_no001" / "card_normal.png"
CARD_FRAME_PATH = ROOT / "data" / "crystal" / "cards_assets" / "cardFrame_L_rarity_2.png"
RARITY_ICON_PATH = ROOT / "data" / "crystal" / "cards_assets" / "rare_star_normal.png"
ATTRIBUTE_ICON_PATH = (
    ROOT / "data" / "crystal" / "cards_assets" / "icon_attribute_mysterious_88.png"
)


if __name__ == "__main__":
    image = compose_card_image(
        card_path=CARD_PATH,
        card_frame_path=CARD_FRAME_PATH,
        rarity_icon_path=RARITY_ICON_PATH,
        rarity_count=2,
        attribute_icon_path=ATTRIBUTE_ICON_PATH,
    )
    image.save(OUTPUT_PATH)
    print(f"rendered: {OUTPUT_PATH}")
