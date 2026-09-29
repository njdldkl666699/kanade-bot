# SDK Issue 草稿

提交地址：<https://github.com/github/copilot-sdk/issues/new>

## 复测记录（2026-09-29）

- **SDK 1.0.14 → 1.0.15，CLI 1.0.85 → 1.0.89：两个问题均未修复。**
- 问题 1：全部变体的上游请求体仍无任何 token 限制字段（completions 顶层字段仍为 `messages/model/reasoning_effort/temperature`；responses 变体为 `include/input/instructions/model/prompt_cache_key/reasoning/store/tools`，新增 `prompt_cache_key` 但仍无 `max_output_tokens`）。RPC 层确认 SDK 1.0.15 仍在 `session.create` 中转发 `provider.maxOutputTokens` / `models[].maxOutputTokens` / `modelCapabilities.limits.maxOutputTokens`，即依旧是运行时丢弃。
  - 附带发现：`wire_api="responses"` 下新版运行时会发送 `summary` 字段，SenseNova 上游返回 400 `json: unknown field "summary"`（上游兼容性问题，与本 issue 主诉无关但影响 responses 变体复测）。
- 问题 2：六个变体结果与 1.0.85 完全一致 —— `caps_camel`/`caps_snake`/`prov_caps_snake` 仍为 128000（且 snake_case 透传也无效，说明不只是 casing 问题，运行时根本不消费 `session.open` 的 `modelCapabilities.limits`）；`prov_direct` 与 `bot_path`（本仓 workaround）仍为 1048576。
  - SDK 1.0.15 的 `ProviderConfig` 仍未暴露 `max_context_window_tokens`，`_convert_provider_to_wire_format` 也不透传；`ModelLimitsOverride` 仍序列化为 camelCase，而 CLI 1.0.89 的 `api.schema.json` 中 `ModelCapabilitiesOverrideLimits` 仍声明 snake_case —— casing 不匹配依旧。

---

**Title:** BYOK `provider.maxOutputTokens` never reaches the upstream request body (chat completions & responses)

## Description

When using a BYOK (OpenAI-compatible) provider, the `max_output_tokens` option documented on `ProviderConfig` is accepted and forwarded to the runtime over JSON-RPC, but the runtime never includes it in the request body sent to the upstream provider — under any configuration path I could find. The same applies to `modelCapabilities.limits.maxOutputTokens` and to `ProviderModelConfig.maxOutputTokens`.

The docs for `ProviderConfig.maxOutputTokens` say:

> Overrides the resolved model's default max output tokens. When hit, the model stops generating and returns a truncated response.

As observed, no truncation limit is ever communicated to the provider, so the provider's own default applies instead.

## Environment

- `github-copilot-sdk` (Python): **1.0.14**
- Runtime bundle: CLI **1.0.85** (`~/.cache/github-copilot-sdk/cli/1.0.85`), also reproduced with the VS Code-bundled runtime
- OS: Linux x64
- Upstream: an OpenAI-compatible `/v1/chat/completions` endpoint (SenseNova), verified to accept and honor `max_tokens`

## Repro

```python
import asyncio
from copilot import CopilotClient

async def main():
    client = CopilotClient()
    session = await client.create_session(
        model="deepseek-flash",
        provider={
            "type": "openai",
            "base_url": "http://127.0.0.1:39221/v1",  # local recording proxy
            "api_key": "sk-...",
            "max_output_tokens": 2345,
        },
        available_tools=[],
    )
    await session.send("hi")
    await session.disconnect()
    await client.stop()

asyncio.run(main())
```

The proxy sits between the SDK and the real provider and records request bodies verbatim (no TLS issues, plain HTTP hop).

## Evidence

**1. The Python SDK does forward the option.** Tracing the `session.create` JSON-RPC request shows:

```json
{
  "method": "session.create",
  "params": {
    "model": "deepseek-flash",
    "provider": {
      "type": "openai",
      "baseUrl": "http://127.0.0.1:39221/v1",
      "apiKey": "sk-...",
      "maxOutputTokens": 2345
    }
  }
}
```

**2. The upstream request body does not contain it.** Captured `POST /v1/chat/completions` (the runtime uses an embedded OpenAI JS client — `User-Agent: OpenAI/JS 5.20.1`):

```json
{
  "model": "deepseek-flash",
  "messages": [...],
  "reasoning_effort": "high",
  "temperature": 1
}
```

No `max_tokens`, `max_completion_tokens`, or `max_output_tokens` — the field is silently dropped by the runtime.

## Variants tested

| Configuration path                                                                                | wire API    | `max_tokens` in upstream body |
| ------------------------------------------------------------------------------------------------- | ----------- | ----------------------------- |
| `provider["max_output_tokens"]`                                                                   | completions | no                            |
| `provider["max_output_tokens"]` + `model_id="gpt-5.4"` + `wire_model`                             | completions | no                            |
| `model_capabilities=ModelCapabilitiesOverride(limits=ModelLimitsOverride(max_output_tokens=...))` | completions | no                            |
| named providers: `providers=[{...}]` + `models=[{... max_output_tokens: ...}]`                    | completions | no                            |
| `provider["max_output_tokens"]`                                                                   | responses   | no                            |

For the responses wire API the body carries `model/instructions/input/tools/reasoning/store/include` — still no `max_output_tokens`, even though that is the exact parameter name the OpenAI Responses API uses.

## Expected behavior

At minimum, one of:

1. `provider.maxOutputTokens` (and `models[].maxOutputTokens`) is translated into the provider-appropriate request field (`max_completion_tokens`/`max_tokens` for completions, `max_output_tokens` for responses); or
2. The docs clarify that these are runtime-internal limits only (never sent to the provider), and an explicit opt-in is added for putting them on the wire.

## Workaround

Run a local OpenAI-compatible proxy between the SDK and the provider that injects the missing field into the request body (e.g. `inject max_tokens: 4096` when absent). Verified working with SenseNova.

> **Retest 2026-09-29, SDK 1.0.15 + CLI 1.0.89: still not fixed.** All variants still produce upstream bodies without `max_tokens` / `max_completion_tokens` / `max_output_tokens`. The completions body is unchanged (`messages/model/reasoning_effort/temperature`); the responses body now includes `prompt_cache_key` but still no token limit. RPC trace confirms SDK 1.0.15 still forwards all the options on `session.create`, so the runtime is still the component dropping them.

---

**Title:** `modelCapabilities.limits.maxContextWindowTokens` is ignored by the runtime; only the undocumented `provider.maxContextWindowTokens` works

## Description

`create_session(model_capabilities=ModelCapabilitiesOverride(limits=ModelLimitsOverride(max_context_window_tokens=...)))` is documented as "Override individual model capabilities resolved by the runtime", but it has no effect on the session's context-window management: `session.history.compact` keeps reporting `contextWindow.tokenLimit = 128000` (the BYOK fallback default), so auto-compaction triggers far too early for long-context BYOK models.

Two additional problems compound this:

1. The Python SDK serializes `ModelLimitsOverride` to **camelCase** (`maxContextWindowTokens`), while the runtime's own `api.schema.json` declares `ModelCapabilitiesOverrideLimits` properties in **snake_case** (`max_context_window_tokens`) — so even if the runtime honored the override, the wire names would not match. Sending snake_case manually (by monkey-patching `_capabilities_to_dict`) changes nothing, so the parameter appears to be entirely non-functional.
2. The only working path — the BYOK `ProviderConfig` top-level `maxContextWindowTokens` — is **not exposed** by the Python SDK: `ProviderConfig` (TypedDict) lacks the field and `_convert_provider_to_wire_format` drops it from the dict.

## Environment

- `github-copilot-sdk` (Python): **1.0.14** (latest on PyPI at the time)
- Runtime bundle: CLI **1.0.85**
- OS: Linux x64

## Evidence (all variants, same BYOK model `deepseek-flash`)

| Configuration path                                                                             | `history.compact` → `contextWindow.tokenLimit` |
| ---------------------------------------------------------------------------------------------- | ---------------------------------------------- |
| (none — baseline)                                                                              | 128000                                         |
| `model_capabilities` limits `maxContextWindowTokens` (SDK default, camelCase wire)             | 128000 ❌                                       |
| `model_capabilities` limits `max_context_window_tokens` (snake_case wire via monkey-patch)     | 128000 ❌                                       |
| `provider.modelCapabilities.limits.max_context_window_tokens` (wire injected via monkey-patch) | 128000 ❌                                       |
| `provider.maxContextWindowTokens` (wire injected via monkey-patch)                             | **1048576 ✅**                                  |

## Expected behavior

- `session.open`'s `modelCapabilities.limits.maxContextWindowTokens` should override the resolved context window used for compaction/truncation/token display (and the Python SDK's wire casing should match what the runtime deserializes); or
- the Python SDK should expose `provider.max_context_window_tokens` (`ProviderConfig` field + wire conversion), since that is the only knob the runtime honors today.

## Workaround

Monkey-patch `CopilotClient._convert_provider_to_wire_format` to pass `maxContextWindowTokens` through on the provider object. Verified working (`tokenLimit = 1048576`). Repro script: `tests/copilot_sdk/test_max_context_window.py`.

> **Retest 2026-09-29, SDK 1.0.15 + CLI 1.0.89: still not fixed.** All six variants reproduce identically: `caps_camel` / `caps_snake` / `prov_caps_snake` → 128000; `prov_direct` / `bot_path` → 1048576. Notably the snake_case passthrough variant still shows the override is not merely a casing mismatch — the runtime does not consume `session.open` `modelCapabilities.limits` at all. SDK 1.0.15's `ProviderConfig` still lacks `max_context_window_tokens` and `_convert_provider_to_wire_format` still drops it; CLI 1.0.89's `api.schema.json` still declares `ModelCapabilitiesOverrideLimits` in snake_case while the SDK sends camelCase.
