from nonebot import on_command, require
from nonebot.permission import SUPERUSER

require("command_counter")

from kanade_bot.plugins.command_counter import register_matcher

add_gallery = on_command(
    "添加画廊",
    aliases={"add_gallery"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(add_gallery, "添加画廊")

remove_gallery = on_command(
    "删除画廊",
    aliases={"remove_gallery"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_gallery, "删除画廊")

add_gallery_alias = on_command(
    "添加画廊别名",
    aliases={"add_gallery_alias", "添加别名"},
    priority=2,
    block=True,
)
register_matcher(add_gallery_alias, "添加画廊别名")

remove_gallery_alias = on_command(
    "删除画廊别名",
    aliases={"remove_gallery_alias", "删除别名"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_gallery_alias, "删除画廊别名")

gallery_pictures = on_command(
    "看所有",
    aliases={"gallery_pictures", "画廊图片列表", "图片一览"},
    priority=2,
    block=True,
)
register_matcher(gallery_pictures, "看所有")

get_picture = on_command(
    "获取图片",
    aliases={"get_picture", "看"},
    priority=3,
    block=True,
)
register_matcher(get_picture, "获取图片")

add_picture = on_command(
    "添加图片",
    aliases={"add_picture", "上传图片", "上传"},
    priority=2,
    block=True,
)
register_matcher(add_picture, "添加图片")

remove_picture = on_command(
    "删除图片",
    aliases={"remove_picture"},
    priority=2,
    permission=SUPERUSER,
    block=True,
)
register_matcher(remove_picture, "删除图片")
