# OpenAI Proxy

独立的 OpenAI 兼容 API 中转服务。接受任意 `POST /v1/*` 端点（`chat/completions`、`responses` 等），转发到配置的上游提供商并保留对应子路径。默认不修改请求或响应；当 `model_supports_images` 为 `false` 时，会在发送前移除 `role: user` 消息中的 `image_url`/`input_image` 内容块。

典型用途：

- 在 Copilot SDK / CLI 的 BYOK `provider.base_url` 与真实提供商之间插入一层，抓取或改写原始请求体
- 为不支持图片输入的模型剥离图片内容块
- 补齐上游请求缺失的参数（如 `max_tokens`，见下方「请求字段注入」）

## 运行

```bash
go run . -config config-example.yaml
```

普通响应会完整转发状态码、响应头和 JSON；上游返回 `text/event-stream` 时，在未安装响应 hook 的情况下逐块转发并保持 SSE 流式特性。

## 配置

参见 `config-example.yaml`，说明：

- `upstream.api_key` 非空时以它设置 `Authorization: Bearer`；为空时透传入站请求的 `Authorization` 头

## 请求记录

设置 `record_file`（JSONL 路径）后，每个上游请求会被原样追加记录为一行 JSON：

```json
{"ts":"2026-09-22T20:00:00+08:00","headers":{"Authorization":"<redacted>","Content-Type":"application/json"},"body":{...原始请求体...}}
```

敏感请求头（`Authorization`、`Cookie`、`Proxy-Authorization`）自动脱敏；无法解析为 JSON 的请求体存入 `body_raw`。可用于抓取 LLM 提供商的原始请求体，例如检查 `max_tokens` 等参数是否生效。

## 状态码重试

设置 `retry.statuses` 后，命中这些上游状态码的响应会按等待-重发循环处理，直到成功、超过 `max_attempts`（返回最后一次响应，不吞错误）或客户端断开。典型场景：`429 tpm exhausted` 之类的分钟级配额限流。

等待时长：

1. 优先采用标准 `Retry-After` 头（秒数或 HTTP-date，OpenAI/Anthropic/GitHub 等均发送），封顶 `backoff_max`，可用 `respect_retry_after: false` 关闭
2. 头不存在或无法解析时，按 `backoff_initial` 起步每次指数翻倍，封顶 `backoff_max`

安全性：429/5xx 发生在响应头阶段（body 尚未开始转发），原样重发无副作用；重试等待期间客户端断开连接会立即中止。总时长受 `timeout` 约束。

## 请求字段注入

设置 `inject_request` 后，代理会在请求体顶层缺失对应字段时注入配置值（已有字段不覆盖）。例如 Copilot BYOK 场景下注入 `max_tokens: 4096` 限制输出长度（Copilot 运行时不会把 BYOK 配置的 `max_output_tokens` 写入上游请求体，实测见 `tests/copilot_sdk/`）。注意不同提供商的参数名：OpenAI 兼容 completions API 为 `max_tokens`（或 `max_completion_tokens`），Responses API 为 `max_output_tokens`。

## Hook 扩展

实现 `RequestHook.BeforeRequest` 可在上游请求前修改 JSON；实现 `ResponseHook.AfterResponse` 可在响应体返回前修改数据。通过 `NewProxy` 创建代理后调用 `AddRequestHook`/`AddResponseHook` 注册。为保证响应 hook 能处理完整结果，存在响应 hook 时流式响应会先缓冲再返回。
