# Chat Completions Proxy

独立的 OpenAI 兼容 Chat Completions 中转服务。请求发送到配置的上游模型提供商，默认不修改请求或响应；当 `model_supports_images` 为 `false` 时，会在发送前移除 `role: user` 消息中的 `image_url`/`input_image` 内容块。

## 运行

```bash
go run . -config config-example.yaml
```

服务端点：`POST /v1/chat/completions`。普通响应会完整转发状态码、响应头和 JSON；上游返回 `text/event-stream` 时，在未安装响应 hook 的情况下逐块转发并保持 SSE 流式特性。

## Hook 扩展

实现 `RequestHook.BeforeRequest` 可在上游请求前修改 JSON；实现 `ResponseHook.AfterResponse` 可在响应体返回前修改数据。通过 `NewProxy` 创建代理后调用 `AddRequestHook`/`AddResponseHook` 注册。为保证响应 hook 能处理完整结果，存在响应 hook 时流式响应会先缓冲再返回。
