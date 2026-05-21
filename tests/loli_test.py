from pathlib import Path

import httpx

url_resp = httpx.get("https://www.loliapi.com/bg/?type=url")
url = url_resp.text
resp = httpx.get(url)
Path("loli.png").write_bytes(resp.content)
# 不一定是美少女图片
