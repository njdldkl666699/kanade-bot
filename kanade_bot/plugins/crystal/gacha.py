import base64
import random
from io import BytesIO
from pathlib import Path

from nonebot import get_driver, logger, require
from PIL import Image
from pydantic import BaseModel, RootModel
from pydantic.alias_generators import to_camel

from .config import cfg as scoped_cfg
from .enum import CHARACTERS, AttrEnum, RarityEnum

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import template_to_pic

cfg = scoped_cfg.gacha

CARD_SIZE = (940, 530)
ATTRIBUTE_ICON_POSITION = (815, 0)
RARITY_ICON_X = 33
RARITY_ICON_BOTTOM_MARGIN = 22
RARITY_ICON_SPACING = 66


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

    pass


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
    """渲染一张卡牌，返回PIL Image对象"""
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


def _image_to_data_url(image: Image.Image) -> str:
    bytes_io = BytesIO()
    image.save(bytes_io, format="PNG")
    encoded = base64.b64encode(bytes_io.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


async def render_gacha_10_cards(cards: list[Card]) -> bytes:
    """渲染10连抽卡牌，返回图片字节流"""
    images = [
        {
            "src": _image_to_data_url(render_composed_card(card)),
            "alt": f"{index}. {card.prefix} {card.character_name}",
        }
        for index, card in enumerate(cards[:10], start=1)
    ]

    return await template_to_pic(
        template_path=str(cfg.template_dir_path),
        template_name=cfg.template_file,
        templates={"images": images},
    )


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
