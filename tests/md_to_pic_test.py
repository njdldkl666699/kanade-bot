import nonebot

nonebot.init()
nonebot.require("nonebot_plugin_htmlrender")
import nonebot_plugin_htmlrender
from nonebot_plugin_htmlrender import render_markdown

print(render_markdown.__kwdefaults__)
assert render_markdown.__kwdefaults__
render_markdown.__kwdefaults__["width"] = 800
print(nonebot_plugin_htmlrender.render_markdown.__kwdefaults__)
