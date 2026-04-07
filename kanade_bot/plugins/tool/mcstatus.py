import base64
import textwrap
from io import BytesIO
from pathlib import Path

from mcstatus.responses import JavaStatusResponse
from nonebot import get_plugin_config
from PIL import Image, ImageDraw, ImageFont

from .config import Config

cfg = get_plugin_config(Config)


def load_icon(icon_data: str | None) -> Image.Image:
    if icon_data and "," in icon_data:
        try:
            raw = base64.b64decode(icon_data.split(",", 1)[1])
            return Image.open(BytesIO(raw)).convert("RGBA")
        except Exception:
            pass

    fallback = Path(__file__).resolve().parents[3] / "grass_block.png"
    return Image.open(fallback).convert("RGBA")


def get_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_path = Path(cfg.tool_font_path)
    if font_path.exists():
        return ImageFont.truetype(str(font_path), size)
    return ImageFont.load_default()


def render_mc_status(status: JavaStatusResponse, host: str, port: int | None = None) -> bytes:
    icon = load_icon(status.icon)
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

    lines = [
        f"地址: {host}" + (f":{port}" if port else ""),
        f"延迟: {status.latency:.1f} ms",
        f"版本: {status.version.name}",
        f"玩家: {players.online}/{players.max}",
        *sample_names,
    ]

    motd_line_height = 30
    line_height = 34
    image_width = 860
    info_start_y = 48 + len(motd_lines) * motd_line_height

    image_height = info_start_y + max(160, line_height * (len(lines) + 1))
    card = Image.new("RGBA", (image_width, image_height), (20, 28, 42, 255))
    draw = ImageDraw.Draw(card)
    draw.rounded_rectangle(
        (24, 24, image_width - 24, image_height - 24), radius=20, fill=(36, 49, 66, 255)
    )
    draw.rounded_rectangle((52, 52, 196, 196), radius=16, fill=(28, 38, 52, 255))
    card.paste(icon, (60, 60), icon)

    for idx, line in enumerate(motd_lines):
        draw.text(
            (230, 40 + idx * motd_line_height), line, fill=(255, 255, 255, 255), font=get_font(30)
        )

    start_y = info_start_y
    for idx, line in enumerate(lines):
        draw.text(
            (230, start_y + idx * line_height), line, fill=(215, 225, 240, 255), font=get_font(24)
        )

    buffer = BytesIO()
    card.convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()
