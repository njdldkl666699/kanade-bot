# SDK Issue 草稿

提交地址：<https://github.com/github/copilot-sdk/issues/new>

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
