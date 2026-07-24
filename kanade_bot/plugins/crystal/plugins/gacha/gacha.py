import random
from pathlib import Path

from nonebot import get_driver, logger
from PIL import Image, ImageDraw, ImageFilter, ImageOps
from pydantic import BaseModel, RootModel
from pydantic.alias_generators import to_camel

from .config import cfg
from .enum import CHARACTERS, AttrEnum, RarityEnum

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


class Card(BaseModel):
    """卡牌信息模型

    示例
    ```json
    {
        "id": 9,
        "characterId": 3,
        "cardRarityType": "rarity_1",
        "attr": "mysterious",
        "prefix": "对所有人温柔的优等生",
        "assetbundleName": "res003_no001",
    }
    """

    id: int
    character_id: int
    card_rarity_type: RarityEnum
    attr: AttrEnum
    prefix: str
    assetbundle_name: str

    @property
    def character_name(self) -> str:
        return CHARACTERS[self.character_id - 1]

    model_config = {
        "alias_generator": to_camel,
        "populate_by_name": True,
    }


class Cards(RootModel[list[Card]]):
    """卡牌列表模型"""


CARDS: dict[RarityEnum, list[Card]] = {rarity: [] for rarity in RarityEnum}
"""卡牌信息字典，键为稀有度枚举，值为对应稀有度的卡牌列表"""


def _open_rgba(path: Path) -> Image.Image:
    return Image.open(path).convert("RGBA")


def _fit_to_card(image: Image.Image) -> Image.Image:
    if image.size == CARD_SIZE:
        return image
    return image.resize(CARD_SIZE, Image.Resampling.LANCZOS)


def card_file_name(trained: bool = False) -> str:
    """获取卡牌图片文件名"""
    if trained:
        return "card_after_training.png"
    return "card_normal.png"


def render_composed_card(card: Card) -> Image.Image:
    """渲染一张卡牌"""
    show_trained = card.card_rarity_type.can_train and cfg.show_trained
    render_file_name = card_file_name(show_trained)

    # 检查缓存
    cache_rendered_file = f"{card.assetbundle_name}_{render_file_name}"
    cache_file_path = cfg.rendered_cards_dir_path / cache_rendered_file
    if cache_file_path.is_file():
        return _open_rgba(cache_file_path)

    # 卡牌图片路径
    card_path = cfg.member_small_dir_path / card.assetbundle_name / render_file_name
    # 稀有度边框图片路径
    card_frame_L_path = cfg.cards_assets_dir_path / card.card_rarity_type.card_frame_L
    # 稀有度图标路径和数量
    rarity_icon, rarity_count = card.card_rarity_type.rarity_icon(show_trained)
    rarity_icon_path = cfg.cards_assets_dir_path / rarity_icon
    # 卡牌属性图标路径
    icon_attribute_88_path = cfg.cards_assets_dir_path / card.attr.icon_attribute_88

    image = _fit_to_card(_open_rgba(card_path)).copy()
    frame = _fit_to_card(_open_rgba(card_frame_L_path))
    rarity_icon = _open_rgba(rarity_icon_path)
    attribute_icon = _open_rgba(icon_attribute_88_path)

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

    # 保存到缓存
    cache_file_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(cache_file_path, format="PNG")

    return image


async def render_gacha_10_cards(cards: list[Card]) -> Image.Image:
    """渲染10连抽卡牌"""
    thumbnail_width, thumbnail_height = GACHA_THUMBNAIL_SIZE
    canvas_width = (
        GACHA_PADDING * 2 + GACHA_COLUMNS * thumbnail_width + (GACHA_COLUMNS - 1) * GACHA_GAP
    )
    canvas_height = GACHA_PADDING * 2 + GACHA_ROWS * thumbnail_height + (GACHA_ROWS - 1) * GACHA_GAP
    canvas = Image.new("RGBA", (canvas_width, canvas_height), GACHA_BACKGROUND)

    shadow_mask = Image.new("L", canvas.size)
    shadow_draw = ImageDraw.Draw(shadow_mask)
    for index in range(min(len(cards), GACHA_COLUMNS * GACHA_ROWS)):
        row, column = divmod(index, GACHA_COLUMNS)
        x = GACHA_PADDING + column * (thumbnail_width + GACHA_GAP)
        y = GACHA_PADDING + row * (thumbnail_height + GACHA_GAP)
        shadow_draw.rounded_rectangle(
            (
                x,
                y + GACHA_SHADOW_OFFSET,
                x + thumbnail_width,
                y + thumbnail_height + GACHA_SHADOW_OFFSET,
            ),
            radius=GACHA_SLOT_RADIUS,
            fill=41,
        )
    shadow_mask = shadow_mask.filter(ImageFilter.GaussianBlur(GACHA_SHADOW_BLUR))
    shadow = Image.new("RGBA", canvas.size, (50, 38, 66, 0))
    shadow.putalpha(shadow_mask)
    canvas.alpha_composite(shadow)

    corner_mask = Image.new("L", GACHA_THUMBNAIL_SIZE)
    ImageDraw.Draw(corner_mask).rounded_rectangle(
        (0, 0, thumbnail_width - 1, thumbnail_height - 1),
        radius=GACHA_SLOT_RADIUS,
        fill=255,
    )

    for index, card in enumerate(cards[: GACHA_COLUMNS * GACHA_ROWS]):
        row, column = divmod(index, GACHA_COLUMNS)
        x = GACHA_PADDING + column * (thumbnail_width + GACHA_GAP)
        y = GACHA_PADDING + row * (thumbnail_height + GACHA_GAP)
        thumbnail = ImageOps.fit(
            render_composed_card(card).convert("RGBA"),
            GACHA_THUMBNAIL_SIZE,
            method=Image.Resampling.LANCZOS,
        )
        slot = Image.new("RGBA", GACHA_THUMBNAIL_SIZE, "white")
        slot.alpha_composite(thumbnail)
        slot.putalpha(corner_mask)
        canvas.alpha_composite(slot, (x, y))

    return canvas


def gacha_draw_card(cumulative_probabilities: dict[RarityEnum, float]) -> Card:
    """进行一次抽卡，返回抽到的卡牌"""
    cumulative_p = random.random()
    rarity = None

    for r, cp in cumulative_probabilities.items():
        if cumulative_p < cp:
            rarity = r
            break
    if rarity is None:
        raise ValueError("抽卡失败，未能匹配到任何稀有度。请检查 gacha_probabilities 配置。")

    card_list = CARDS.get(rarity, [])
    if not card_list:
        raise ValueError(
            f"抽卡失败，稀有度 {rarity.value} 没有可用的卡牌。请联系管理员检查卡牌配置。"
        )
    return random.choice(card_list)


driver = get_driver()


@driver.on_startup
def load_cards():
    """在插件启动时加载卡牌信息"""
    p = Path(cfg.card_info_file_path)
    if not p.is_file():
        logger.warning("卡牌信息文件不存在: {}", p)
        return

    cards_data = Cards.model_validate_json(p.read_text(encoding="utf-8")).root
    for card in cards_data:
        CARDS[card.card_rarity_type].append(card)
