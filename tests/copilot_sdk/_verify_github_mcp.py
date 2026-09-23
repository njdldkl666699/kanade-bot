"""验证 BYOK 会话默认工具列表中是否包含内置 GitHub MCP 工具。

前置：启动 openai-proxy（config-record.yaml，监听 :39221）。

创建 BYOK 会话（provider 指向本地 proxy），**不带任何工具过滤**
（不传 available_tools / excluded_tools，mcp_servers 也不传），
发送一条消息后检查发往上游的请求体 tools 数组。
"""

import asyncio
import json
from pathlib import Path

import yaml
from copilot import CopilotClient, SessionEvent
from copilot.session import SystemMessageConfig
from copilot.session_events import AssistantMessageData, SessionErrorData, SessionIdleData

ROOT = Path(__file__).resolve().parents[2]
CAPTURE_FILE = Path(__file__).parent / "captured_requests.jsonl"
PROXY_BASE_URL = "http://127.0.0.1:39221/v1"

EXPECTED_PREFIXES = ("github", "mcp__github")


async def run_variant(client: CopilotClient, name: str, extra: dict) -> None:
    idle = asyncio.Event()
    contents: list[str] = []

    def handler(event: SessionEvent) -> None:
        match event.data:
            case AssistantMessageData() as data:
                contents.append(data.content)
            case SessionErrorData() as data:
                print(f"session error: {data.message}")
                idle.set()
            case SessionIdleData():
                idle.set()

    session = await client.create_session(
        session_id=f"sdk-test-{name}-{int(asyncio.get_event_loop().time())}",
        system_message={"mode": "replace", "content": "你是测试助手，请只回复：OK"},
        client_name="kanade-bot-sdk-test",
        model=cfg["sensenova-deepseek-flash"]["model"],
        provider=provider,
        **extra,
    )
    unsub = session.on(handler)
    try:
        await session.send(f"[{name}] 请只回复：OK")
        await asyncio.wait_for(idle.wait(), timeout=300)
    finally:
        unsub()
        await session.disconnect()
    print(f"[{name}] assistant: {''.join(contents)[:40]!r}")


VARIANTS = [
    ("default", {}),
    ("disabled_mcp", {"disabled_mcp_servers": ["github-mcp-server"]}),
    ("excluded_prefix", {"excluded_tools": ["mcp:github-mcp-server"]}),
    ("excluded_wild", {"excluded_tools": ["mcp:github-mcp-server:*"]}),
]


async def main() -> None:
    global cfg, provider
    cfg = yaml.safe_load((ROOT / "config-prod.yaml").read_text(encoding="utf-8"))
    provider = {**cfg["sensenova-deepseek-flash"]["provider"], "base_url": PROXY_BASE_URL}

    for name, extra in VARIANTS:
        client = CopilotClient(
            client_info={"application_name": "kanade_bot_sdk_test", "application_version": "0"}
        )
        try:
            print(f"\n>>> {name} ...")
            await run_variant(client, name, extra)
        finally:
            await client.stop()

    # 逐变体解析捕获的请求（每个变体取最后一条，倒序匹配）
    lines = CAPTURE_FILE.read_text(encoding="utf-8").strip().splitlines()
    seen: dict[str, list] = {}
    for line in reversed(lines):
        body = json.loads(line)["body"]
        marker = next(
            (
                v
                for v, _ in VARIANTS
                for m in body.get("messages", [])
                if f"[{v}]" in str(m.get("content", ""))
            ),
            None,
        )
        if marker and marker not in seen:
            seen[marker] = [
                t.get("function", {}).get("name") or t.get("name") for t in body.get("tools", [])
            ]
    for name, _ in VARIANTS:
        tools = seen.get(name)
        if tools is None:
            print(f"{name:<14} 未捕获请求")
            continue
        github = [t for t in tools if "github" in str(t).lower()]
        print(f"{name:<14} tools={len(tools):>2}  github工具={len(github):>2} {github[:3]}")


if __name__ == "__main__":
    asyncio.run(main())
