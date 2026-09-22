"""测试 GitHub Copilot SDK 的 ``max_output_tokens`` 是否进入上游请求体。

前置条件
--------
1. 启动记录版 openai-proxy（独立端口 :39221，原样转发并记录请求体）::

    cd openai-proxy && go run . -config config-record.yaml

2. 运行本脚本::

    .venv/bin/python tests/copilot_sdk/test_max_output_tokens.py

原理
----
从 ``config-prod.yaml`` 读取 ``sensenova-deepseek-flash`` 的 Provider/模型配置，
仅把 ``provider.base_url`` 指向本地 proxy；proxy 把请求原样转发到 sensenova，
并将原始请求体记录到 ``captured_requests.jsonl``。

测试变体（各自使用醒目的 max_output_tokens 值，便于区分来源）：

- ``baseline``     : 不传任何 max_output_tokens（对照组）
- ``capabilities`` : 复现 summarizer.py 当前路径，通过
  ``model_capabilities.limits.max_output_tokens = 1234`` 传递
- ``provider``     : 通过 ``provider.max_output_tokens = 2345`` 传递
- ``provider_mid`` : provider.max_output_tokens = 2345 且 ``model_id="gpt-5.4"``，
  验证「运行时只对已知模型计算并发送 max_tokens」假说
- ``named_models`` : 通过 ``models=[{... max_output_tokens: 3456}]`` BYOK 模型定义传递

同时会把 ``session.create`` 的 JSON-RPC 参数存到 ``rpc_trace.jsonl``，
用于区分「Python SDK 未传递」和「运行时收到但未使用」。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, ClassVar

import yaml
from copilot import CopilotClient, SessionEvent
from copilot._jsonrpc import JsonRpcClient
from copilot.session import (
    ModelCapabilitiesOverride,
    ModelLimitsOverride,
    ModelSupportsOverride,
    SystemMessageConfig,
)
from copilot.session_events import AssistantMessageData, SessionErrorData, SessionIdleData

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "config-prod.yaml"
CAPTURE_FILE = Path(__file__).parent / "captured_requests.jsonl"
RPC_TRACE_FILE = Path(__file__).parent / "rpc_trace.jsonl"
MODEL_CONFIG_KEY = "sensenova-deepseek-flash"
PROXY_BASE_URL = "http://127.0.0.1:39221/v1"
TOKEN_FIELDS = ("max_tokens", "max_completion_tokens", "max_output_tokens")
VARIANT_TAGS = ("baseline", "capabilities", "provider_mid", "named_models", "responses", "provider")
_ONLY = {s for s in os.environ.get("ONLY", "").split(",") if s}
"""ONLY 环境变量：逗号分隔，只运行指定变体；默认全量运行并清空历史捕获"""


def _err_exit(msg: str) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(1)


def install_rpc_trace() -> None:
    """记录 session.create / session.model.switchTo 发出的完整参数"""
    original = JsonRpcClient.request

    async def traced(self, method: str, params: dict | None = None, *args: Any, **kwargs: Any):
        if method in ("session.create", "session.model.switchTo"):
            entry = {"method": method, "params": params}
            with RPC_TRACE_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
        return await original(self, method, params, *args, **kwargs)

    JsonRpcClient.request = traced


def load_model_config() -> dict:
    """加载 config-prod.yaml 中 sensenova-deepseek-flash 的展开配置"""
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    cfg = data[MODEL_CONFIG_KEY]
    assert cfg["provider"]["base_url"] == "https://token.sensenova.cn/v1/", cfg["provider"]
    return cfg


def build_caps(max_output_tokens: int | None) -> ModelCapabilitiesOverride:
    """构造与生产一致的 ModelCapabilitiesOverride，max_output_tokens 可选"""
    return ModelCapabilitiesOverride(
        supports=ModelSupportsOverride(vision=False, reasoning_effort=True),
        limits=ModelLimitsOverride(
            max_context_window_tokens=1048576,
            max_output_tokens=max_output_tokens,
        ),
    )


async def send_and_wait(session, prompt: str, *, timeout: float = 300.0) -> list[str]:
    """发送消息并等待 session.idle，返回全部助手消息内容（同 summarizer.py）"""
    idle_event = asyncio.Event()
    error: list[Exception] = []
    contents: list[str] = []

    def handler(event: SessionEvent) -> None:
        match event.data:
            case AssistantMessageData() as data:
                contents.append(data.content)
            case SessionIdleData():
                idle_event.set()
            case SessionErrorData() as data:
                error.append(RuntimeError(f"Session error: {data.message or data}"))
                idle_event.set()

    unsubscribe = session.on(handler)
    try:
        await session.send(prompt)
        await asyncio.wait_for(idle_event.wait(), timeout=timeout)
        if error:
            raise error[0]
        return contents
    finally:
        unsubscribe()


class Harness:
    """与 summarizer.Summarizer 等价的会话创建路径"""

    system_message: ClassVar[SystemMessageConfig] = {
        "mode": "replace",
        "content": "你是测试助手，请用一句话简短回答。",
    }

    def __init__(self, model_cfg: dict):
        self.model_cfg = model_cfg

    async def run_variant(self, name: str, session_extra: dict, caps: ModelCapabilitiesOverride):
        """创建会话并发送一条消息，session_extra 直接合并进 create_session 参数"""
        client = CopilotClient(
            client_info={"application_name": "kanade_bot_sdk_test", "application_version": "0"}
        )
        try:
            create_kwargs: dict = {
                "session_id": f"sdk-test-{name}-{int(asyncio.get_event_loop().time())}",
                "system_message": self.system_message,
                "client_name": "kanade-bot-sdk-test",
                "model": self.model_cfg["model"],
                "provider": {**self.model_cfg["provider"], "base_url": PROXY_BASE_URL},
                "reasoning_effort": self.model_cfg["reasoning_effort"],
                "model_capabilities": caps,
                "available_tools": [],
            }
            create_kwargs.update(session_extra)
            session = await client.create_session(**create_kwargs)
            try:
                contents = await send_and_wait(session, f"[{name}] 请只回复：OK")
            finally:
                await session.disconnect()
            print(f"[{name}] 助手响应: {''.join(contents)[:50]!r}")
        finally:
            await client.stop()


def analyze() -> int:
    """分析捕获的请求体，输出每个变体的 token 限制字段"""
    print("\n" + "=" * 70)
    print("抓包结果分析")
    print("=" * 70)
    lines = CAPTURE_FILE.read_text(encoding="utf-8").strip().splitlines()
    if not lines:
        print("未捕获到任何请求！")
        return 1

    failures = []
    for i, line in enumerate(lines, 1):
        entry = json.loads(line)
        body = entry.get("body")
        if not isinstance(body, dict):
            print(f"#{i}: 非标准请求体: {entry.get('body_raw', body)!r:.80}")
            continue
        # prompt 中带有变体标记 [name]（兼容 completions 的 messages 与 responses 的 input）
        texts = [str(msg.get("content", "")) for msg in body.get("messages", [])]
        for item in body.get("input", []):
            if isinstance(item, dict):
                for part in item.get("content", []) or []:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif isinstance(part, str):
                        texts.append(part)
        marker = next((tag for tag in VARIANT_TAGS if any(tag in t for t in texts)), "?")
        token_parts = {f: body.get(f) for f in TOKEN_FIELDS if f in body}
        print(
            f"#{i} 变体={marker:<12} model={body.get('model')!r} "
            f"reasoning_effort={body.get('reasoning_effort')!r}\n"
            f"    顶层字段: {sorted(body)}\n"
            f"    token限制字段: {token_parts or '（无）'}"
        )
        if marker == "capabilities" and not token_parts:
            failures.append("capabilities 变体：请求体没有携带任何 token 限制字段")
        if marker in ("provider", "provider_mid") and not token_parts:
            failures.append(f"{marker} 变体：请求体未携带 token 限制字段（实际: {token_parts}）")
        if marker == "named_models" and not token_parts:
            failures.append(
                f"named_models 变体：请求体未携带 token 限制字段（实际: {token_parts}）"
            )

    print("-" * 70)
    if failures:
        print("结论（异常项）:")
        for f in failures:
            print(f"  ✗ {f}")
    else:
        print("结论: 所有预期均满足")
    return 0


async def main() -> int:
    if not _ONLY:
        # 全量运行时清空历史捕获，避免与旧记录混淆
        CAPTURE_FILE.unlink(missing_ok=True)
        RPC_TRACE_FILE.unlink(missing_ok=True)
    install_rpc_trace()

    model_cfg = load_model_config()
    harness = Harness(model_cfg)
    api_key = model_cfg["provider"]["api_key"]

    provider_max = {
        "provider": {
            **model_cfg["provider"],
            "base_url": PROXY_BASE_URL,
            "max_output_tokens": 2345,
        }
    }
    provider_mid = {
        "provider": {
            **model_cfg["provider"],
            "base_url": PROXY_BASE_URL,
            "max_output_tokens": 2345,
            "model_id": "gpt-5.4",
            "wire_model": model_cfg["model"],
        }
    }
    named_models = {
        "provider": None,
        "model": "sensenova/deepseek-flash",
        "providers": [
            {
                "name": "sensenova",
                "type": "openai",
                "base_url": PROXY_BASE_URL,
                "api_key": api_key,
            }
        ],
        "models": [
            {
                "id": model_cfg["model"],
                "provider": "sensenova",
                "max_output_tokens": 3456,
            }
        ],
    }

    provider_responses = {
        "provider": {
            **model_cfg["provider"],
            "base_url": PROXY_BASE_URL,
            "max_output_tokens": 2345,
            "wire_api": "responses",
        }
    }

    variants = [
        # (名称, create_session 额外参数, model_capabilities)
        ("baseline", {}, build_caps(max_output_tokens=None)),
        ("capabilities", {}, build_caps(max_output_tokens=1234)),
        ("provider", provider_max, build_caps(max_output_tokens=None)),
        ("provider_mid", provider_mid, build_caps(max_output_tokens=None)),
        ("named_models", named_models, build_caps(max_output_tokens=None)),
        ("responses", provider_responses, build_caps(max_output_tokens=None)),
    ]
    selected = [v for v in variants if not _ONLY or v[0] in _ONLY]
    for name, session_extra, caps in selected:
        print(f"\n>>> 运行变体 {name} ...")
        await harness.run_variant(name, session_extra, caps)

    return analyze()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
