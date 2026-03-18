from typing import Any

from pydantic import BaseModel, Field


class McServerPlugin(BaseModel):
    name: str
    version: str | None = None


class McServerPlayerSample(BaseModel):
    name: str
    id: str


class McServerData(BaseModel):
    hostname: str | None = None
    port: int | None = None
    status: bool = False
    version: str | None = None
    protocol: int | None = None
    server_title: str | None = None
    players: int | None = None
    max_players: int | None = None
    motd: str | None = None
    motd_raw: str | None = None
    favicon: str | None = None
    mods: list[Any] = Field(default_factory=list)
    software: str | None = None
    plugins: list[McServerPlugin] = Field(default_factory=list)
    ping: int | None = None
    query_method: str | None = None
    error: str | None = None
    players_sample: list[McServerPlayerSample] = Field(default_factory=list)
    map: str | None = None
    game_mode: str | None = None
    description: dict[str, Any] | None = None
    debug_log: list[str] = Field(default_factory=list)


class McServerResponse(BaseModel):
    code: int
    msg: str
    address: str
    data: McServerData


def format_mc_server_data(data: McServerData) -> str:
    players = "未知"
    if data.players is not None and data.max_players is not None:
        players = f"{data.players}/{data.max_players}"

    plugin_text = "无"
    if data.plugins:
        plugin_text = "、".join(
            f"{plugin.name}({plugin.version})" if plugin.version else plugin.name
            for plugin in data.plugins[:10]
        )
        if len(data.plugins) > 10:
            plugin_text += f" 等{len(data.plugins)}个"

    sample_players = "无"
    if data.players_sample:
        sample_players = "、".join(player.name for player in data.players_sample[:10])
        if len(data.players_sample) > 10:
            sample_players += f" 等{len(data.players_sample)}个"

    lines = [
        "Minecraft服务器信息",
        f"服务器：{data.hostname}:{data.port}",
        f"版本：{data.version or '未知'}",
        f"人数：{players}",
        f"延迟：{f'{data.ping}ms' if data.ping is not None else '未知'}",
        f"地图：{data.map or '未知'}",
        f"模式：{data.game_mode or '未知'}",
        f"描述：{data.motd or '无'}",
        f"插件：{plugin_text}",
        f"玩家：{sample_players}",
    ]
    return "\n".join(lines)
