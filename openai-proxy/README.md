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

## 配置热重载

服务启动后通过 [viper](https://github.com/spf13/viper)（fsnotify）监听 `-config` 指向的配置文件，保存后自动重载，无需重启。行为细节：

- **触发**：直接覆盖写入，以及编辑器常见的「临时文件写入 + rename 原子替换」均可触发；连续事件防抖约 200ms 后统一重载
- **解析**：与启动加载共用同一份 `loadConfig`（yaml.v3），语义完全一致；解析失败（如文件写到一半被读到）时按 500ms 间隔重试至多 3 次，仍失败则保留旧配置并等待下一次变更
- **生效方式**：代理的运行时状态（配置、上游 client、hooks）按新配置整体重建后原子替换，在途请求继续用旧状态完成，不会出现半新半旧的组合；重载日志会逐项列出变更（`api_key` 只提示变化、不打印值）
- **立即生效**：`upstream.*`、`timeout`、`retry`、`inject_request`、`record_file`、`fix_reasoning_final`
- **需要重启**：`listen`——监听地址已随进程绑定，变更只会在日志中提示

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

安全性：429/5xx 发生在响应头阶段（body 尚未开始转发），原样重发无副作用；重试等待期间客户端断开连接会立即中止。每次尝试等待上游响应头的时长受 `timeout` 约束（见下方「超时语义」）。

## 超时语义

`timeout`（默认 5m）只约束**每次尝试等待上游响应头**的时间（含重试循环中的每一次请求）。流式响应的 body 不设总时长上限——LLM 的长生成可以合法地远超 5m，若按总时长掐断，Copilot 运行时会把被截断的流当作错误并整段静默重试，造成重复生成与迟到的“僵尸回复”。

流式 body 的中止由客户端断开驱动：宿主（如 kanade-bot 在聊天会话超时后调用 Copilot SDK 的 `session.abort`）断开连接时，代理随请求上下文取消在途上游请求与重试等待，上游生成随之中止，不再浪费 token。

## 请求字段注入

设置 `inject_request` 后，代理会在请求体顶层缺失对应字段时注入配置值（已有字段不覆盖）。例如 Copilot BYOK 场景下注入 `max_tokens: 4096` 限制输出长度（Copilot 运行时不会把 BYOK 配置的 `max_output_tokens` 写入上游请求体，实测见 `tests/copilot_sdk/`）。注意不同提供商的参数名：OpenAI 兼容 completions API 为 `max_tokens`（或 `max_completion_tokens`），Responses API 为 `max_output_tokens`。

## reasoning_content 通道修复

设置 `fix_reasoning_final: true` 后，修复 DeepSeek 思考模式「写错输出通道」的问题：模型偶尔会把最终答案整体写入 `reasoning_content`（`content` 为空、`finish_reason` 仍为 `stop`），并在结尾用 `Final:\n` 分隔推理与答案（DeepSeek V4 Pro / V4.1 Flash 流式 + 思考模式下有约三成复现率）。

处理规则（仅作用于 chat completions 形状的响应，按最后一个 `Final:\n` 切分，`Final:` 后必须紧跟换行才命中，避免误匹配正文提及）：

- `content` 为空（缺失、null 或纯空白）且 `reasoning_content` 含 `Final:\n` 时，标记后内容搬入 `content`，`reasoning_content` 保留标记前的推理
- 流式与非流式响应均支持；流式按 delta 累积判定切点后重写受影响的帧，未受影响的帧字节不变
- 标记后没有内容（如被 `max_tokens` 截断在标记处）时不改动

代价：启用后存在响应 hook，**流式响应会先完整缓冲再转发**，客户端失去增量输出（见「Hook 扩展」）。

## Hook 扩展

实现 `RequestHook.BeforeRequest` 可在上游请求前修改 JSON；实现 `ResponseHook.AfterResponse` 可在响应体返回前修改数据。通过 `NewProxy` 创建代理后调用 `AddRequestHook`/`AddResponseHook` 注册。为保证响应 hook 能处理完整结果，存在响应 hook 时流式响应会先缓冲再返回。
