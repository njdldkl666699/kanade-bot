import base64
from pathlib import Path
from typing import Literal

from mcstatus.responses import JavaStatusResponse
from nonebot import get_plugin_config, require

from .config import Config

require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import template_to_pic

cfg = get_plugin_config(Config)


def _encode_image_as_data_url(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    mime_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }.get(suffix, "image/png")
    raw = image_path.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime_type};base64,{b64}"


def _resolve_icon_src(icon_data: str | None) -> str:
    if icon_data and "," in icon_data:
        try:
            base64.b64decode(icon_data.split(",", 1)[1])
            return icon_data
        except Exception:
            pass

    fallback = Path(cfg.tool_fallback_icon_file_path)
    return _encode_image_as_data_url(fallback)


type Theme = Literal["light", "dark"]


def _get_latency_level(value: float, theme: Theme) -> Literal["good", "warn", "bad"]:
    if theme == "light":
        if value < 100:
            return "good"
        if value < 250:
            return "warn"
        return "bad"
    if theme == "dark":
        if value < 100:
            return "good"
        if value < 250:
            return "warn"
        return "bad"


async def render_mc_status(
    status: JavaStatusResponse,
    address: str,
    theme: Theme = "light",
) -> bytes:
    motd_html = status.motd.to_html()
    if not motd_html or motd_html.strip() in {"", "<p></p>"}:
        motd_html = "<p>我的世界服务器状态</p>"

    players = status.players
    sample = players.sample
    sample_names: list[str] = []
    if sample:
        sample_names = [player.name for player in sample]

    latency = status.latency

    template_path = Path(cfg.tool_template_file_path)
    template_name = template_path.name

    return await template_to_pic(
        template_path=str(template_path.resolve().parent),
        template_name=template_name,
        templates={
            "theme": theme,
            "icon_src": _resolve_icon_src(status.icon),
            "motd_html": motd_html,
            "address": address,
            "latency": f"{latency:.1f}",
            "latency_level": _get_latency_level(latency, theme),
            "version_name": status.version.name,
            "online_players": players.online,
            "max_players": players.max,
            "sample_names": sample_names,
        },
        wait=50,
    )
