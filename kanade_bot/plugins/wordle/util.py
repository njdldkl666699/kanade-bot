from enum import Enum
from io import BytesIO
from typing import Annotated

from nonebot.adapters.onebot.v11 import MessageEvent
from nonebot.params import Depends
from PIL import ImageFont
from PIL.Image import Image as IMG

from kanade_bot.utils.session import extract_session_info_sync


def get_session_id(event: MessageEvent) -> str:
    return extract_session_info_sync(event).session_id


SessionId = Annotated[str, Depends(get_session_id)]


class GuessResult(Enum):
    WIN = 0
    """猜出正确结果"""
    LOSS = 1
    """达到最大可猜次数，未猜出正确结果"""
    DUPLICATE = 2
    """猜测重复"""
    ILLEGAL = 3
    """猜测不合法"""


def save_png(frame: IMG) -> BytesIO:
    output = BytesIO()
    frame = frame.convert("RGBA")
    frame.save(output, format="png")
    return output


def load_font(font_name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(font_name, size=size)
    except OSError:
        return ImageFont.load_default(size=size)
