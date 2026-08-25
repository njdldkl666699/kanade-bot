import nonebot


nonebot.init()
nonebot.require("nonebot_plugin_htmlrender")

from nonebot_plugin_htmlrender import md_to_pic

print(md_to_pic.__defaults__)
assert md_to_pic.__defaults__
new_defaults = list(md_to_pic.__defaults__)
new_defaults[3] = 800  # 修改默认宽度为 800
md_to_pic.__defaults__ = tuple(new_defaults)
print(md_to_pic.__defaults__)
