import base64
import textwrap
from io import BytesIO
from pathlib import Path
from typing import Literal

from mcstatus.responses import JavaStatusResponse
from nonebot import get_plugin_config
from PIL import Image, ImageDraw, ImageFont

from .config import Config

cfg = get_plugin_config(Config)


def _load_icon(icon_data: str | None) -> Image.Image:
    if icon_data and "," in icon_data:
        try:
            raw = base64.b64decode(icon_data.split(",", 1)[1])
            return Image.open(BytesIO(raw)).convert("RGBA")
        except Exception:
            pass

    fallback = Path(cfg.tool_fallback_icon_path)
    return Image.open(fallback).convert("RGBA")


def _get_font(size: float) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = Path(cfg.tool_font_path)
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default(size)


type Theme = Literal["light", "dark"]
type RGBA = tuple[int, int, int, int]

_palettes: dict[str, dict[str, RGBA]] = {
    "dark": {
        "background": (20, 28, 42, 255),
        "card": (36, 49, 66, 255),
        "icon_panel": (28, 38, 52, 255),
        "motd_text": (255, 255, 255, 255),
        "info_text": (215, 225, 240, 255),
    },
    "light": {
        "background": (238, 243, 250, 255),
        "card": (252, 254, 255, 255),
        "icon_panel": (224, 234, 245, 255),
        "motd_text": (24, 36, 52, 255),
        "info_text": (58, 72, 90, 255),
    },
}


def _get_latency_color(value: float, theme: Theme) -> RGBA:
    if theme == "light":
        if value < 100:
            return (42, 134, 77, 255)
        if value < 250:
            return (189, 117, 24, 255)
        return (196, 56, 56, 255)
    if theme == "dark":
        if value < 100:
            return (86, 201, 120, 255)
        if value < 250:
            return (255, 170, 64, 255)
        return (255, 92, 92, 255)


def render_mc_status(
    status: JavaStatusResponse,
    host: str,
    port: int | None = None,
    theme: Theme = "light",
) -> bytes:
    palette = _palettes[theme]

    icon = _load_icon(status.icon)
    icon = icon.resize((128, 128))

    description = status.motd.to_plain()
    motd_lines: list[str] = []
    if description:
        lines = description.splitlines()
        for line in lines:
            motd_lines.extend(textwrap.wrap(line.strip(), width=40))
    else:
        motd_lines.append("我的世界服务器状态")

    players = status.players
    sample = players.sample
    sample_names: list[str] = []
    if sample:
        sample_names = [player.name for player in sample]

    latency = status.latency

    lines = [
        f"地址: {host}" + (f":{port}" if port else ""),
        f"延迟: {latency:.1f} ms",
        f"版本: {status.version.name}",
        f"玩家: {players.online}/{players.max}",
        *sample_names,
    ]

    IMAGE_WIDTH = 860
    MOTD_LINE_HEIGHT = 30
    LINE_HEIGHT = 34

    info_start_y = 48 + len(motd_lines) * MOTD_LINE_HEIGHT
    image_height = info_start_y + max(160, LINE_HEIGHT * (len(lines) + 1))

    card = Image.new("RGBA", (IMAGE_WIDTH, image_height), palette["background"])
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (24, 24, IMAGE_WIDTH - 24, image_height - 24),
        radius=20,
        fill=palette["card"],
    )
    draw.rounded_rectangle(
        (52, 52, 196, 196),
        radius=16,
        fill=palette["icon_panel"],
    )
    card.paste(icon, (60, 60), icon)
    # MOTD文本
    for idx, line in enumerate(motd_lines):
        draw.text(
            (230, 40 + idx * MOTD_LINE_HEIGHT),
            line,
            fill=palette["motd_text"],
            font=_get_font(30),
        )
    # 信息文本
    start_y = info_start_y
    for idx, line in enumerate(lines):
        color = palette["info_text"]
        if line.startswith("延迟:"):
            color = _get_latency_color(latency, theme)
        draw.text(
            (230, start_y + idx * LINE_HEIGHT),
            line,
            fill=color,
            font=_get_font(24),
        )

    buffer = BytesIO()
    card.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
