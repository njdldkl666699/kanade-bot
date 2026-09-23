"""实测 Copilot SDK ``max_context_window_tokens`` 是否被运行时应用。

问题背景
--------
config 中 ``model_capabilities.limits.max_context_window_tokens: 1048576``
已随 ``create_session(model_capabilities=...)`` 传给运行时，但会话压缩
返回的 ``context_window.token_limit`` 仍是默认 128000。

本脚本用本地 mock OpenAI server（SSE 流式）离线复现，逐变体对比
``session.history.compact`` 返回的 ``token_limit``：

- ``baseline``      : 不传任何 capabilities（对照组，预期默认 128000）
- ``caps_camel``    : SDK 正常路径 → wire ``limits.maxContextWindowTokens``
                      （camelCase，与运行时 schema 的 snake_case 疑似不匹配）
- ``caps_snake``    : monkeypatch 透传 snake_case ``limits.max_context_window_tokens``
- ``prov_direct``   : provider wire 追加顶层 ``maxContextWindowTokens``
- ``prov_caps_snake``: provider wire 追加 ``modelCapabilities.limits.max_context_window_tokens``

运行::

    .venv/bin/python tests/copilot_sdk/test_max_context_window.py [variant ...]
"""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from copilot import CopilotClient, SessionEvent
from copilot.rpc import SessionHistoryCompactRequest, Trigger
from copilot.session import ModelCapabilitiesOverride, ModelLimitsOverride
from copilot.session_events import (
    AssistantMessageData,
    SessionErrorData,
    SessionIdleData,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MOCK_PORT = 39301
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}/v1"
TARGET = 1048576
RPC_LOG = Path(__file__).parent / "mcw_rpc_trace.jsonl"
RESULT_LOG = Path(__file__).parent / "mcw_results.json"


# ---------------------------------------------------------------------------
# Mock OpenAI SSE server
# ---------------------------------------------------------------------------
async def start_mock_server() -> asyncio.AbstractServer:
    """极简 OpenAI /v1/chat/completions SSE mock，固定回复 OK"""
    chunk = lambda delta, finish=None: {  # noqa: E731
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # 读请求头与 body
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
            if req.get("stream"):
                payload = (
                    "data: "
                    + json.dumps(chunk({"role": "assistant"}))
                    + "\n\ndata: "
                    + json.dumps(chunk({"content": "OK"}))
                    + '\n\ndata: {"id":"chatcmpl-mock","object":"chat.completion.chunk",'
                    '"created":0,"model":"mock","choices":[{"index":0,"delta":{},'
                    '"finish_reason":"stop"}],"usage":{"prompt_tokens":10,'
                    '"completion_tokens":1,"total_tokens":11}}\n\n'
                    "data: [DONE]\n\n"
                ).encode()
                ctype = "text/event-stream"
            else:
                payload = json.dumps(
                    {
                        "id": "chatcmpl-mock",
                        "object": "chat.completion",
                        "created": 0,
                        "model": "mock",
                        "choices": [
                            {
                                "index": 0,
                                "message": {"role": "assistant", "content": "OK"},
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 10,
                            "completion_tokens": 1,
                            "total_tokens": 11,
                        },
                    }
                ).encode()
                ctype = "application/json"
            writer.write(
                f"HTTP/1.1 200 OK\r\nContent-Type: {ctype}\r\n"
                "Cache-Control: no-cache\r\nConnection: close\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", MOCK_PORT)


# ---------------------------------------------------------------------------
# RPC trace：记录 session.create 参数中 capabilities/provider 相关字段
# ---------------------------------------------------------------------------
def install_rpc_trace() -> None:
    from copilot._jsonrpc import JsonRpcClient

    original = JsonRpcClient.request

    async def traced(self, method: str, params: dict | None = None, *a: Any, **kw: Any):
        if method in ("session.create", "session.model.switchTo"):
            slim = {}
            if params:
                slim["provider"] = params.get("provider")
                slim["modelCapabilities"] = params.get("modelCapabilities")
                slim["model"] = params.get("model")
            with RPC_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"method": method, "params": slim}, ensure_ascii=False) + "\n")
        return await original(self, method, params, *a, **kw)

    JsonRpcClient.request = traced


async def send_and_wait(session, prompt: str, *, timeout: float = 120.0) -> list[str]:
    idle = asyncio.Event()
    err: list[Exception] = []
    texts: list[str] = []

    def on_event(event: SessionEvent) -> None:
        match event.data:
            case AssistantMessageData() as d:
                texts.append(d.content)
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
        return texts
    finally:
        unsub()


async def run_variant(
    name: str,
    *,
    caps=None,
    provider_extra_wire: dict | None = None,
    bot_model_key: str | None = None,
):
    """创建会话→发两条消息→compact→读取 token_limit"""
    import copilot.client as cc

    orig_caps_fn = cc._capabilities_to_dict
    orig_prov_fn = cc.CopilotClient._convert_provider_to_wire_format
    if name == "caps_snake":
        # 透传 snake_case dict（绕过 SDK 的 camelCase 序列化）
        cc._capabilities_to_dict = lambda c: dict(c)
    if provider_extra_wire:

        def patched(self, provider):
            wire = orig_prov_fn(self, provider)
            wire.update(provider_extra_wire)
            return wire

        cc.CopilotClient._convert_provider_to_wire_format = patched

    session_config: dict = {}
    if bot_model_key:
        # bot 真实路径：nonebot初始化→kanade schema→model_dump_session_config
        # （自动回填 provider.max_context_window_tokens）→utils/copilot.py 真实patch
        import nonebot

        cwd = os.getcwd()
        os.chdir(ROOT)  # get_project_version() 读项目根的 pyproject.toml
        try:
            try:
                nonebot.get_driver()
            except Exception:
                nonebot.init(driver="~httpx")
            from kanade_bot.utils.copilot import _patch_provider_wire_conversion
        finally:
            os.chdir(cwd)
        import yaml

        from kanade_bot.utils.schema import BaseAgentConfig

        with open(Path(__file__).resolve().parents[2] / "config-prod.yaml") as f:
            data = yaml.safe_load(f)
        raw = {
            k: v
            for k, v in data[bot_model_key].items()
            if k not in ("disabled_mcp_servers", "mcp_servers", "excluded_tools")
        }
        raw["system_prompt_file"] = "x.md"
        agent_cfg = BaseAgentConfig.model_validate(raw)
        session_config = agent_cfg.model_dump_session_config()
        _patch_provider_wire_conversion()
        # base_url 改指 mock，避免真实外网调用；其余字段原样保留
        session_config["provider"]["base_url"] = MOCK_BASE
        session_config.pop("additional_directories", None)

    client = CopilotClient(
        client_info={"application_name": "kanade_mcw_test", "application_version": "0"}
    )
    try:
        kwargs: dict = {
            "session_id": f"mcw-{name}",
            "system_message": {"mode": "replace", "content": "你是测试助手。"},
            "client_name": "mcw-test",
            "model": "deepseek-flash",
            "provider": {"type": "openai", "base_url": MOCK_BASE, "api_key": "mock"},
            "available_tools": [],
        }
        if caps is not None:
            kwargs["model_capabilities"] = caps
        kwargs.update(session_config)
        session = await client.create_session(**kwargs)
        await send_and_wait(session, f"[{name}] 请只回复：OK")
        await send_and_wait(session, f"[{name}] 再回复一次：OK")
        result = await session.rpc.history.compact(
            SessionHistoryCompactRequest(trigger=Trigger.MANUAL), timeout=120
        )
        cw = result.context_window
        print(
            f"[{name}] success={result.success} token_limit="
            f"{cw.token_limit if cw else 'N/A'} current={cw.current_tokens if cw else 'N/A'}"
        )
        return {"variant": name, "token_limit": cw.token_limit if cw else None}
    finally:
        await client.stop()
        cc._capabilities_to_dict = orig_caps_fn
        cc.CopilotClient._convert_provider_to_wire_format = orig_prov_fn


VARIANTS: dict[str, dict] = {
    "baseline": {},
    "caps_camel": {
        "caps": ModelCapabilitiesOverride(
            limits=ModelLimitsOverride(max_context_window_tokens=TARGET)
        )
    },
    "caps_snake": {"caps": {"limits": {"max_context_window_tokens": TARGET}}},
    "prov_direct": {"provider_extra_wire": {"maxContextWindowTokens": TARGET}},
    "prov_caps_snake": {
        "provider_extra_wire": {
            "modelCapabilities": {"limits": {"max_context_window_tokens": TARGET}}
        }
    },
    # bot 真实完整路径：config-prod.yaml → BaseAgentConfig →
    # model_dump_session_config（自动回填）→ utils/copilot.py patch → wire
    "bot_path": {"bot_model_key": "sensenova-deepseek-flash"},
}


async def main() -> None:
    selected = sys.argv[1:] or list(VARIANTS)
    if selected == list(VARIANTS):
        RPC_LOG.unlink(missing_ok=True)
    server = await start_mock_server()
    orig_cwd = os.getcwd()
    tmpdir = tempfile.mkdtemp(prefix="mcw_test_")
    os.chdir(tmpdir)  # 避免运行时在仓库里写会话文件
    results = []
    try:
        install_rpc_trace()
        for name in selected:
            v = VARIANTS[name]
            try:
                r = await run_variant(name, **v)
                results.append(r)
            except Exception as e:  # noqa: BLE001
                import traceback

                print(f"[{name}] FAILED: {e}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                results.append({"variant": name, "error": str(e)})
    finally:
        os.chdir(orig_cwd)
        server.close()
    RESULT_LOG.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print("\n===== summary =====")
    for r in results:
        print(r)


if __name__ == "__main__":
    asyncio.run(main())
