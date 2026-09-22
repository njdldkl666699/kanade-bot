"""直接向 sensenova 发送带 max_tokens 的请求，验证上游接受该字段"""

import json

import httpx
import yaml

cfg = yaml.safe_load(open("config-prod.yaml", encoding="utf-8"))
p = cfg["sensenova-deepseek-flash"]["provider"]

body = {
    "model": "deepseek-flash",
    "messages": [{"role": "user", "content": "请只回复：OK"}],
    "max_tokens": 16,
}
r = httpx.post(
    f"{p['base_url'].rstrip('/')}/chat/completions",
    headers={"Authorization": f"Bearer {p['api_key']}"},
    json=body,
    timeout=120,
)
print("status:", r.status_code)
data = r.json()
print("usage:", json.dumps(data.get("usage", {}), ensure_ascii=False))
print("choices[0].finish_reason:", data["choices"][0].get("finish_reason"))
print("content:", data["choices"][0]["message"].get("content", "")[:50])
