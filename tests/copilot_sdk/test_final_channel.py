"""实测 Copilot 运行时对 DeepSeek「Final 通道」异常的处理链路。

问题背景
--------
DeepSeek V4.1 Flash 偶尔把最终答案整体写入 ``reasoning_content``
（``content`` 为空），并以 ``"Final:\\n"`` 分隔推理与答案；上游已开始
对历史中的空 assistant 消息报
``400 Invalid assistant message: content or tool_calls must be set``。

本脚本用本地 mock OpenAI server 离线复现，回答两个问题：

1. 运行时给 bot 的 ``AssistantMessageData`` 里 ``content`` /
   ``reasoning_text`` / ``reasoning_wire_field`` 各是什么（bot 层能否
   提取答案）？
2. 下一轮请求回传的历史里，这条异常 assistant 消息长什么样
   （content 是否为空、reasoning 是否回传）？

运行::

    .venv/bin/python tests/copilot_sdk/test_final_channel.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from copilot import CopilotClient, SessionEvent
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionIdleData,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MOCK_PORT = 39309
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"
REQ_LOG = Path(__file__).parent / "final_channel_requests.jsonl"

REASONING = "让我想想该怎么回答。\nFinal:\n这是被写错通道的最终答案：42"

# mock行为状态：按用户消息文本记录「无标记空响应」是否已触发过一次
_empty_once_seen: set[str] = set()


def _mock_message(last_user: str) -> dict:
    """根据最后一条用户消息选择响应：
    - 含「无标记」：首次返回空响应（reasoning无任何标记），其后正常
    - 含「始终空」：始终返回带Final标记的空响应（验证重试耗尽后的兜底）
    - 其余：正常回复
    """
    if "无标记" in last_user:
        if "无标记" not in _empty_once_seen:
            _empty_once_seen.add("无标记")
            return {
                "role": "assistant",
                "content": "",
                "reasoning_content": "推理过程没有任何标记",
            }
        return {"role": "assistant", "content": "重发后的正常回复"}
    if "始终空" in last_user:
        return {"role": "assistant", "content": "", "reasoning_content": REASONING}
    return {"role": "assistant", "content": "正常的回复"}


async def start_mock_server() -> asyncio.AbstractServer:
    """按最后一条用户消息内容返回正常/异常响应的 mock OpenAI server。"""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin1").splitlines()
            content_length = 0
            for ln in lines:
                if ln.lower().startswith("content-length:"):
                    content_length = int(ln.split(":", 1)[1])
            body_raw = await reader.readexactly(content_length) if content_length else b""
            try:
                req = json.loads(body_raw) if body_raw else {}
            except Exception:  # noqa: BLE001
                req = {}
            with REQ_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(req, ensure_ascii=False) + "\n")

            messages = req.get("messages", [])
            last_user = next(
                (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            message = _mock_message(str(last_user))

            payload = json.dumps(
                {
                    "id": "chatcmpl-mock",
                    "object": "chat.completion",
                    "created": 0,
                    "model": "mock",
                    "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            ).encode()
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except Exception:  # noqa: BLE001, S110
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", MOCK_PORT)


async def send_and_wait(session, prompt: str, *, timeout: float = 120.0):
    idle = asyncio.Event()
    events: list[AssistantMessageData] = []
    err: list[Exception] = []

    def on_event(event: SessionEvent) -> None:
        match event.data:
            case AssistantMessageData() as d:
                events.append(d)
            case SessionIdleData():
                idle.set()
            case SessionErrorData() as d:
                err.append(RuntimeError(f"Session error: {d.message or d}"))
                idle.set()

    unsub = session.on(on_event)
    try:
        await session.send(prompt)
        await asyncio.wait_for(idle.wait(), timeout=timeout)
        if err:
            raise err[0]
        return events
    finally:
        unsub()


def import_bot_helper():
    """导入bot真实代码路径（需nonebot.init + 项目根cwd，参照mcw测试）。"""
    import nonebot

    global EmptyResponseError

    cwd = os.getcwd()
    os.chdir(ROOT)  # get_project_version() 读项目根的 pyproject.toml
    try:
        try:
            nonebot.get_driver()
        except Exception:  # noqa: BLE001
            nonebot.init(driver="~httpx")
        from kanade_bot.utils.copilot import (
            EmptyResponseError,
            copilot_send_and_wait_stream,
        )

        return copilot_send_and_wait_stream
    finally:
        os.chdir(cwd)


def print_requests(tag: str, requests: list[dict]) -> None:
    print(f"== {tag}（共{len(requests)}个请求，打印各请求的messages）:")
    for i, req in enumerate(requests, 1):
        print(f"   -- 请求{i}:")
        for m in req.get("messages", []):
            slim = {k: (v if len(str(v)) < 80 else str(v)[:80] + "…") for k, v in m.items()}
            print(f"      {slim}")


async def main() -> None:
    REQ_LOG.unlink(missing_ok=True)
    server = await start_mock_server()
    stream_helper = import_bot_helper()
    client = CopilotClient(
        client_info={
            "application_name": "final_channel_test",
            "application_version": "0",
        }
    )
    try:
        await client.start()
        session = await client.create_session(
            session_id="final-channel-test",
            system_message={"mode": "replace", "content": "你是测试助手。"},
            client_name="final-test",
            model="deepseek-flash",
            provider={"type": "openai", "base_url": MOCK_BASE, "api_key": "mock"},
            available_tools=[],
        )

        # 场景A：无标记空响应一次 → 截断本轮+重发，第二次应正常
        base = len(REQ_LOG.read_text().splitlines()) if REQ_LOG.is_file() else 0
        print("== 场景A：无标记空响应（应截断后重发并拿到正常回复）")
        async for d in stream_helper(session, "场景A：无标记空响应", timeout=60):
            print(f"   收到回复: content={d.content!r}")
        reqs = [json.loads(line) for line in REQ_LOG.read_text().splitlines()[base:]]
        print_requests("场景A请求", reqs)

        # 场景B：始终空（带Final标记）→ 重试耗尽后应抛EmptyResponseError
        base = len(REQ_LOG.read_text().splitlines())
        print("== 场景B：始终空响应（应重试耗尽后抛EmptyResponseError）")
        try:
            async for d in stream_helper(session, "场景B：始终空响应", timeout=60):
                print(f"   收到回复: content={d.content!r}")
            print("   !! 未抛异常（不符合预期）")
        except EmptyResponseError as e:
            print(f"   收到异常（预期）: {e}")
        reqs = [json.loads(line) for line in REQ_LOG.read_text().splitlines()[base:]]
        print_requests("场景B请求", reqs)

        # 场景B后续：历史中不应残留空content的assistant消息
        print("== 场景B后续：正常消息的历史（验证空响应已清除）")
        async for d in stream_helper(session, "场景B后续：普通消息", timeout=60):
            print(f"   收到回复: content={d.content!r}")
        last_req = json.loads(REQ_LOG.read_text().splitlines()[-1])
        print("   最后请求的messages:")
        for m in last_req.get("messages", []):
            slim = {k: (v if len(str(v)) < 80 else str(v)[:80] + "…") for k, v in m.items()}
            print(f"      {slim}")
    finally:
        await client.stop()
        server.close()


if __name__ == "__main__":
    asyncio.run(main())
