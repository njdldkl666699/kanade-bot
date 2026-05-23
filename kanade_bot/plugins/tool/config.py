from pathlib import Path

from nonebot import get_plugin_config, require
from nonebot.adapters.onebot.v11 import Message
from pydantic import BaseModel, RootModel

from kanade_bot.utils.config import ensure_json_config, write_json_config

require("nonebot_plugin_localstore")

from nonebot_plugin_localstore import get_plugin_config_file


class ScopedConfig(BaseModel):
    fallback_icon_file: str = "grass_block.png"
    """服务器图标加载失败时的替代图标名，支持PNG格式。"""
    template_file: str = "mcstatus_template.html"
    """服务器状态渲染使用的HTML模板文件名，支持HTML格式。"""
    schedule_configs_file: str = "schedule_configs.json"
    """定时任务配置文件名。暂仅支持OneBot v11群聊。"""
    preset_reaction_config_file: str = "preset_reaction_config.json"
    """预设反应配置文件名"""

    @property
    def fallback_icon_file_path(self) -> Path:
        """服务器图标加载失败时的替代图标路径"""
        return get_plugin_config_file(self.fallback_icon_file)

    @property
    def template_file_path(self) -> Path:
        """服务器状态渲染使用的HTML模板文件路径"""
        return get_plugin_config_file(self.template_file)

    @property
    def schedule_configs_file_path(self) -> Path:
        """定时任务配置文件路径"""
        return get_plugin_config_file(self.schedule_configs_file)

    @property
    def preset_reaction_config_file_path(self) -> Path:
        """预设反应配置文件路径"""
        return get_plugin_config_file(self.preset_reaction_config_file)


class Config(BaseModel):
    tool: ScopedConfig


cfg = get_plugin_config(Config).tool


class ScheduleConfig(BaseModel):
    """定时任务配置"""

    cron: str
    """定时任务的Cron表达式"""
    message: Message
    """定时任务发送的消息内容"""


class ScheduleConfigs(RootModel[dict[int, dict[str, ScheduleConfig]]]):
    """定时任务配置列表

    键为群号，值为该群的定时任务列表：
        键为定时任务名称，每个群内不可重复
        值为定时任务配置
    """


schedules = ensure_json_config(cfg.schedule_configs_file_path, ScheduleConfigs)


def write_schedules():
    """将定时任务配置写入配置文件"""
    write_json_config(cfg.schedule_configs_file_path, schedules)


class PresetReactionConfig(BaseModel):
    """预设反应配置"""

    receive_poke_messages: list[str] = [
        "（被戳一下后，轻轻晃了晃神）",
        "啊…嗯？怎么了？",
        "有点突然…吓到了一下。",
        "…在听。有什么事吗？",
        "（稍微顿了一下）…嗯，我在。",
        "是不是戳错人了…不过，也没关系。",
        "嗯…？找我吗？",
        "…别戳太多次，会有点晕。",
        "啊…抱歉，刚刚在听音乐，没注意到。",
        "…嗯。我在。",
        "如果是想聊天的话…直接说就好。",
    ]
    """收到戳一戳消息时的随机回复列表"""

    send_poke_probability: int = 30
    """发送戳一戳的概率，范围0-100"""

    send_like_messages: list[str] = [
        "今天也给你点了个赞…希望你能稍微开心一点。",
        "嗯…点了赞。只是一个小小的表示。",
        "看到你在努力的样子…就想点一下。",
        "不太擅长说话…但想让你知道，我有在关注。",
        "点个赞…如果有打扰到的话，不用在意也没关系。",
        "嗯，点好了。不用回也没关系。",
        "看到你头像的时候…随手就点了一个。",
        "只是想让你知道，有人在看着这边。",
        "点赞…大概是我能做的、最简单的那种鼓励。",
    ]
    """发送点赞消息时的随机回复列表"""

    send_like_limited_message: str = "好像到上限了。明天再试吧…"
    """触发点赞上限的回复内容"""


preset_reaction_cfg = ensure_json_config(
    path=cfg.preset_reaction_config_file_path,
    config_cls=PresetReactionConfig,
)
