# 会话压缩设计（Compaction）

> 实现位置：`kanade_bot/plugins/chat/agent/compaction.py`
> 本文只讲压缩策略、触发时机、token 估算与预算控制；压缩事件在数据库中的存储与恢复重放见 `SESSION_STORE_DESIGN.md`。

## 1. 职责与不变式

会话压缩负责在上下文逼近模型窗口时收缩历史，使长会话可以无限延续。全链路只有一条最重要的不变式：

> **在线压缩与恢复重放必须共用同一个 `apply_strategy`。**

DB 保留全量原始消息，压缩以事件（mark）形式落库、恢复时重放。两套实现必然漂移，一漂移重放结果就与在线历史不一致，上游前缀（KV）缓存随之失效。所有压缩产物——不管是在线产生还是轮末产生——都必须能表达为「一条 mark + 一段可重放或可快照的历史」。

## 2. 两档策略

采用 `pydantic-ai-harness` 的 `TieredCompaction`，从低档到高档依次尝试，直到满足目标预算：

| 档位 | 策略 | 性质 | mark 存储 |
| --- | --- | --- | --- |
| 零成本档 | `ClearToolResults` | 确定性 + 幂等的纯函数：就地清空旧工具返回内容，消息条数与位置不变，配对完整性由官方保证 | 只存参数，恢复时重放（`clear_tool_results`） |
| 摘要档 | `SummarizingCompaction` | LLM 生成摘要，非确定性，无法重放 | 必须存完整产物快照（`summarizing`） |

两档的设计取向都是**前缀缓存友好**：清工具结果不删消息（打点之后的缓存才失效）；摘要只在零成本档回收不够时才启用。`min_clear_tokens`（生产 2000）在零成本档在线生效——清理收益太小则跳过，避免白白弄坏缓存；恢复重放时忽略该阈值（重放没有 usage 锚点，mark 记录的是「确实清理过」的事实，无条件执行）。

摘要档两个关键参数：`preserve_first_user_message=False`（长期群聊第一条用户消息无意义——历史上曾因 upstream 默认 True 导致「喵喵」复读事故）；`keep_user_messages=True` 时保留被摘要范围内最近的用户消息，`summary_keep_messages` 是用户消息与原样保留的尾部消息共享的名额（用户消息优先）。

## 3. 触发时机：轮末压缩 + 在线应急阀（v3）

当前架构出自两起生产事故（在线估算误触发 + 压缩打断进行中的工具链），核心原则是**常规压缩绝不打断进行中的轮次**：

```
轮次内（run 内）                      轮末（finally，回复落库后）
┌────────────────────────┐          ┌──────────────────────────────┐
│ 在线应急阀              │          │ compact_between_turns         │
│ online_valve_fraction   │          │ 闸门 = summary_target_fraction│
│ (0.92，None=完全关闭)    │          │ (0.8，未启用时退 trigger 0.7) │
│ 超阈值才压缩（罕见）     │          │ clear → summary 依次升级      │
└────────────────────────┘          └──────────────────────────────┘
```

- **轮末压缩**是常规路径：在 `send_and_wait` 的 finally 里执行，传入 run 结束时的完整历史（避免重复 restore），失败只记日志不影响会话状态。此时 usage 锚点就是最终响应、其后没有新消息，天然免疫 run 内「锚点后字符启发式」的误估。
- **在线应急阀**仅防单轮内上下文暴涨（如工具返回巨型内容）击穿窗口：`RecordingCompaction` 包住分层策略，按会话记录 run 内每次压缩的前后快照，run 结束由 `persist_run` 落库。`online_valve_fraction=None` 时用 `_NoopCapability` 占位，记录路径保持统一。记录前后快照必须各自拷贝：`TieredCompaction` 以 `ctx.messages[:] = ...` 原地替换列表，直接引用同一对象会让前后指纹永远相等、压缩事件静默丢失。
- 手动压缩命令（`/压缩`）经 `compact_now` 直接执行，不经过触发判断；`build_summary` 构造中 `max_fraction` 仅为满足构造校验（摘要档未启用时以 1.0 占位）。轮末、应急阀与手动三路共用 `build_clear` / `build_summary` 同一构造，保证策略行为与 mark 重放一致。

## 4. Token 估算

`estimate_request_tokens` 在 harness 通用估算之上做两点增强：

1. **图片按张计**：通用估算不把二进制 payload 当文本计数。以最近一条带 usage 的 `ModelResponse` 为锚点，锚点之后新增的图片按 `IMAGE_TOKEN_ESTIMATE`（1024 tokens/张，DeepSeek 视觉模型单张上限）计入——既不让本轮图片绕过阈值，也不按文件字节数严重高估。
2. **已知缺口**：upstream 对 `ToolReturnPart` 的 `str(content)` 会把 bytes repr 成巨串（工具返回图片曾产生 47 万幻影 tokens 导致误触发）。未在本地修补，靠轮末压缩的锚点天然免疫（见 §3）；真危险场景仅剩「真实占用逼近窗口时应急阀内压缩打断任务」，属可接受的保底行为。

## 5. 请求预算（RequestBudgetCapability）

与压缩并列的另一个 capability，解决「input + max_tokens ≤ window」型 400 错误：

- 每次模型请求前重估输入 tokens，`可用输出 = window − 估算输入 − 安全余量`（余量 = max(8192, 5% window)），动态收缩 `max_tokens`——400 死区由此消除（曾以生产事故实证服务器约束是两者之和不超过窗口）。
- 可用输出低于 `MIN_OUTPUT_TOKENS`（4096）时抛 `ContextBudgetError`，阻止必然越界的请求发出。
- 摘要请求同样经 `summary_model_settings` 继承主模型设置并预留输出预算，预算不足直接失败而非发出截断的摘要。

## 6. 压缩事件落库（persist_run）

`persist_run` 把一次 run 的新增消息与压缩事件编排落库，是重放一致性的枢纽：

1. **无压缩**：`new_messages()`（官方以 run_id 多层回退维护的增量边界，仅在未发生压缩时可靠，此时是官方正确机制）直接追加。
2. **有压缩**：对每个事件补写压缩前未落库的消息（DB 行覆盖到最后一个 pre）→ 记 mark（`up_to_seq` = 当前行数 − 1）→ 压缩后新产生的消息追加为 mark 之后的尾部。恢复时按 `up_to_seq` 分段重放并拼尾部。
3. **对齐用内容级公共前缀而非位置**：pydantic-ai 每次请求前跑 `repair_messages()` 可能缩短/改写历史，位置切片会错位丢消息；公共前缀变短即检测到改写，mark 强制走完整快照（正确性优先，放弃该次重放优化）。前缀比对基于消息指纹的 blob 标记形态，详见 `SESSION_STORE_DESIGN.md` §5。
4. **mark 类型自验证**：零成本档对 pre 用记录参数重放，能复现产物就只存参数（零额外存储）；否则视为摘要产物落快照。轮末压缩同判定。

## 7. 上下文窗口固化

窗口来源优先级：显式配置 `context_window` > `/models` 元数据缓存（重试，不失败缓存…见已知问题）> genai-prices > 回退 200k。manager 初始化时把解析结果 `model_copy` 进压缩参数：

- 固化值进入 mark 指纹——跨进程的窗口漂移（如上游 profile 更新）可被检测为参数漂移，restore 自动丢弃旧 marks 全量重放；
- `TieredCompaction` 不再每请求解析窗口（历史事故：解析在 run 内漂移导致阈值静默变化）。

## 8. 配置（CompactionConfig）

| 字段 | 默认 | 生产值 | 说明 |
| --- | --- | --- | --- |
| `trigger_fraction` | 0.7 | 0.7 | 零成本档触发比例；摘要档启用时不参与 |
| `keep_pairs` | 3 | 3 | 保留最近 N 组工具调用对 |
| `min_clear_tokens` | None | 2000 | 清理收益下限（仅在线生效，重放忽略） |
| `context_window` | None | None | 窗口覆盖值；None 时启动解析并固化 |
| `summary_target_fraction` | None | 0.8 | 摘要档预算比例；None = 不启用摘要档 |
| `summary_model` | None | null | 摘要模型；None 继承主模型 |
| `summary_keep_messages` | 40 | 20 | 摘要范围内保留名额（与用户消息共享） |
| `summary_keep_user_messages` | True | true | 保留被摘要范围内最近用户消息 |
| `online_valve_fraction` | 0.92 | 0.92 | 应急阀阈值；None 完全关闭 |

阈值约束（1M 窗口实测）：最小 ≥0.7（最大群常态占用 66.8%）；在线阀须低于预算拒绝线 ≈0.945。

## 9. 已知限制

- `ContextBudgetError` 无专门捕获路径：用户侧表现为通用错误文案且本轮消息丢失（finally 轮末压缩会隐式自愈占用）。
- 模型元数据获取失败会缓存 `(None, None)`，与「失败不缓存」的 docstring 相悖（存量问题）。
- `summary_keep_user_messages` 保留的用户消息内嵌二进制时原样保留，成本随之（工具返回的图片走 `view_image` 的 tool 通道而非用户消息，清工具结果即自愈，不受此影响）。
