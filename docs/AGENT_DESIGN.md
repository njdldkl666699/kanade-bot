# Agent 总体设计

> 实现位置：`kanade_bot/plugins/chat/`（核心在 `agent/` 子目录）
> 本文档是聊天 Agent 的顶层架构总览。记忆系统、会话存储等模块的内部设计各有专门文档，此处不展开。

## 1. 定位

宵崎奏Bot 是基于 NoneBot2 的多平台聊天机器人（OneBot v11 / Console），其中 chat 插件提供一个具备工具调用、文件操作、持久记忆与长会话能力的对话 Agent，其余插件（水晶、总结、娱乐等）是围绕它的功能生态。

Agent 框架采用 **Pydantic AI**（`pydantic-ai-slim[mcp,openai]` + `pydantic-ai-harness` + `pydantic-ai-backends`），通过 Chat Completions 兼容 API 直连上游（OpenAIChatModel）。历史上经历 github-copilot-sdk → openai-agents → Pydantic AI 两轮全量迁移，2026-10-03 起现行架构稳定运行，迁移期文档已清理。

## 2. 分层结构

```
┌─────────────────────────────────────────────────┐
│ 平台层  bot.py / NoneBot2 / OneBot v11 + Console │  事件、适配器、配置加载（anyconfig YAML）
├─────────────────────────────────────────────────┤
│ 插件层  kanade_bot/plugins/*                     │  chat 为核心，crystal/summary/... 为生态
├─────────────────────────────────────────────────┤
│ 运行时层 plugins/chat/agent/*                    │  会话管理、压缩、沙箱、记忆、工具、提示词
├─────────────────────────────────────────────────┤
│ 模型层  utils/pai_runtime.py                     │  get_model / build_model_settings / 续写
└─────────────────────────────────────────────────┘
```

运行时层各文件职责：

| 文件/子包 | 职责 |
| --- | --- |
| `manager.py` | `ChatSessionManager`：共享运行时（Agent/存储/MCP/沙箱池/压缩配置）持有者、每会话 `ChatSession` 注册表、MCP 连接与缓冲区缓存生命周期钩子；调用方经 `get_session()` 持有会话对象 |
| `session/` | `runtime.py`：`ChatSession`（每会话运行时状态与轮次编排 `send_and_wait`）；`input_build.py`：提示词组装/图片暂存沙箱/多模态内容；`stats.py`：工作区树形列表等统计辅助 |
| `deps.py` | `ChatDeps`：单次运行绑定的本地上下文（会话/发送者、记忆作用域、沙箱句柄） |
| `history/` | `compaction.py` + `budget.py`：分层压缩策略、`RequestBudgetCapability`（动态 max_tokens）、轮末压缩与落库（见 `COMPACTION_DESIGN.md`）；`store.py`/`conv_db.py`/`gate_store.py`/`blobs.py`：append-only 会话数据库（主消息/marks/gate 表共享同一 `ConvDBRegistry`）与图片外置（见 `SESSION_STORE_DESIGN.md`） |
| `memory.py` | 作用域隔离的长期记忆（见 `MEMORY_DESIGN.md`） |
| `sandbox/` | mirage VFS + sandlock 沙箱：`hostenv`（Landlock/uv 检查与降级 wrapper）、`workspace`（`KanadeWorkspace` 与 capability）、`session`（常驻会话与 venv）、`manager`（沙箱池）（见 `SANDBOX_DESIGN.md`） |
| `tools/` | 宿主工具集按域拆分：`media`（图片查看/搜索/发送、HTML 渲染）、`messaging`（语音/文件/转发记录）、`memory`（记忆三件套）、`env`（Python 环境）、`scheduling`（定时任务） |
| `prompt.py` | 模块化提示词渲染（`config/chat/prompts/` 下的 Markdown 片段） |
| `vision.py` / `schedule.py` | 图片转述子 agent；定时任务存储与触发 |
| `proactive/` | 主动回复：`runtime`（编排与统计）、`gate`（决策 Agent 构建）、`gate_history`（决策消息历史）、`windows`（时段概率）、`debounce`（防抖）、`executor`（主动回复执行）（见 `AUTOREPLY_DESIGN.md`） |

插件顶层（接口与共享设施）：`matcher.py`（全部 matcher 集中定义）+ `handler/`（按命令域拆分的处理器：`chat_flow` 被动回复编排与回复资格判定、`session_cmds`/`task_cmds`/`ban`/`auto_reply`/`meme_cmds`）；`config/`（按域拆分的配置包）；`memes/`（表情包语义检索子系统）；`deliver.py`（分段投递）、`rag.py`、`util.py`（轮次计费回调、黑名单判定与群 ID 提取）。

## 3. 一轮对话的生命周期

```
适配器事件
  → matcher.py 规则匹配（唤醒词 / 主动回复启用群 / ban 名单）
  → handler/chat_flow.py：水晶计费检查 → send_message_in_chunks
  → ChatSession.send_and_wait（异步生成器，逐条 yield 助手消息；调用方经 `chat_manager` 薄转发）
      1. 获取每会话 asyncio.Lock；群聊会话在此锁内切换记忆作用域到当前发送者
      2. 组装提示词：RAG 检索片段 + 消息缓冲区 + 引用消息 + 系统通知 + 时间行
      3. 沙箱启用时：获取会话常驻沙箱，用户图片写入工作区并附路径说明
      4. SessionStore.restore：全量历史 + 按 compaction marks 重放压缩
      5. agent.run_stream_events：完整 agent 循环（含工具调用后继续生成）
      6. persist_run：新增消息与压缩事件 append-only 落库
      7. 轮末：压缩 / 摘要（compact_between_turns）
  → 分段发送回平台；on_usage 回调计费
```

流式消费的三个关键点：产出粒度是 `PartEndEvent`（整条助手消息，token 级 delta 不外发）；watchdog 包在 `__anext__` 上，相邻事件超时即 `cancel()` 并抛 `TimeoutError`；取消/异常路径同样保证消息缓冲区清空与会话锁释放（调用方需 `aclosing` 包裹生成器）。

## 4. 核心设计决策

| 决策 | 要点 |
| --- | --- |
| Chat Completions 直连 | 主用上游是 Chat Completions 兼容 API，选 Pydantic AI 即因其对该路径一等公民支持；不再有代理层 |
| 会话并发模型 | 每会话一把锁串行化；`ChatDeps` 每次 run 重建，Agent 与工具静态化；工具循环无请求数上限（`REQUEST_LIMIT=None`，默认 50 不适用长工具链） |
| 会话对象模型 | 全局单 `ChatSessionManager`（`manager.py`）持有重量级共享资源（Agent、会话/记忆存储、MCP 连接、沙箱池、压缩配置、提示词渲染），会话级运行时状态（消息缓冲区、通知槽、流中断引用、会话锁、记忆上下文）内聚到每会话一个的 `ChatSession`（`session/runtime.py`）。会话对象经 `get_session` 注册表获取：get-or-create 为纯同步代码，事件循环内无协程切换、无需锁；`current_stream` 同理为普通属性赋值，`interrupt` 无锁读取。由此每会话仅一把语义纯粹的锁，无全局锁与锁序约束。依赖方向恒为 session → 共享设施：`SandboxManager._sessions`、`ConvDBRegistry._convs` 这类资源域的每会话分片留在各自模块内部（其生命周期锚点是宿主工作区目录/进程级缓存，而非会话运行时对象，随 ChatSession 重建会丢沙箱 shell 状态与缓存收益）。沿革：Copilot SDK 时期为单 Client 多 Session，迁移 Pydantic AI 后长期残留「5 个平行 dict + 全局锁 + 双层锁序」形态，2026-10 拆分；调用方经 `get_session()` 获取会话对象直接调用 |
| 历史与压缩 | DB 保留全量原始消息，压缩事件以 marks 落库、恢复时重放——内存与磁盘上永远是可重放的确定性状态，且保护上游 KV 前缀缓存（详见 `SESSION_STORE_DESIGN.md`） |
| 压缩策略 | TieredCompaction：先 `ClearToolResults`（零成本、前缀缓存友好），不够再 `SummarizingCompaction`；轮末例行压缩 + 在线应急阀（详见 `COMPACTION_DESIGN.md`） |
| 沙箱 | mirage 进程内 VFS（约 0.1MB/会话）+ sandlock（Landlock/seccomp）做边界，取代 Docker；`KanadeWorkspace` 把相对路径绑定到每会话工作区根；低内核自动降级 Landlock ABI（详见 `SANDBOX_DESIGN.md`） |
| 工具体系 | 宿主工具为普通可调用对象，经 `RunContext[ChatDeps]` 取上下文；MCP 工具集（搜索类）per-run 挂载（`Agent.toolsets` 只读，不能运行期改） |
| 记忆 | 自研 SQLite MemoryStore，`(platform, user_id)` / `(platform, group_id)` 作用域隔离；SDK 原生记忆无法表达此边界，不混用（详见 `MEMORY_DESIGN.md`） |
| 提示词 | `config/chat/` 下 Markdown 文件模块化组织，`PromptRenderer` 渲染为系统/会话双层指令 |
| 上下文预算 | `RequestBudgetCapability` 按窗口动态收缩 max_tokens，消除「input+max_tokens≤window」约束下的 400 死区 |

## 5. 可靠性机制

- **空响应重发**：整轮无文本且无工具调用时，按消息空间基准（`baseline`）与 DB 行基准（`db_floor`）双重回退后重发，最多 2 次；耗尽抛 `EmptyResponseError`（本轮已回退，不污染历史）
- **截断续写**：`finish_reason == "length"` 时以一句续写提示继续，单轮最多 5 次；续写历史已入库，回退点不受影响
- **超时即真停**：流事件间隔超时触发进程内 `cancel()`，在途 LLM 请求与工具循环一并中断，无僵尸 turn
- **用量回调**：仅轮次正常结束时回调 `on_usage`（累计含重发与续写），供计费与统计

## 6. 子 Agent 与生态

chat 主 agent 之外还有两个独立 `Agent` 实例共用 `pai_runtime` 模型层：`image_caption`（图片转述，无工具）与 summary 插件的会话总结器（压缩摘要也复用同一套构建函数）。

`schedule.py` 是"被动应答"之外唯一的主动出口。agent 经 `schedule_task` 工具创建一次性定时任务，任务持久化为 JSON，重启恢复、过期跳过。到点由 APScheduler 回调以系统通知唤醒会话运行一轮，回复经 `deliver` 主动发回原会话。目标会话已拉黑或创建者水晶余额为负则不唤醒直接放弃；触发失败有限重试，耗尽丢弃；唤醒的运行按实际 usage 向创建者计费。用户命令「任务列表 / 取消任务」可查询与取消。

## 7. 代码地图

```
bot.py                          # 入口：配置加载、NoneBot 初始化、适配器与插件注册
kanade_bot/plugins/chat/
├── matcher.py                  # 全部 matcher 集中定义（与其他插件约定一致）
├── handler/                    # 按命令域拆分的处理器；chat_flow 为被动回复编排
├── config/                     # 按域拆分的配置包（__init__ 汇总再导出）
├── memes/                      # 表情包语义检索子系统（索引/打标/embedding）
├── deliver.py                  # 分段投递（被动回复与主动发送共用）
├── rag.py / util.py
└── agent/                      # 见 §2 职责表（session/history/sandbox/tools/proactive/...）
kanade_bot/utils/pai_runtime.py # 模型层（实例缓存、ModelSettings、续写提示）
docs/MEMORY_DESIGN.md           # 记忆系统设计
docs/SESSION_STORE_DESIGN.md    # 会话存储设计
docs/COMPACTION_DESIGN.md       # 会话压缩设计
docs/SANDBOX_DESIGN.md          # 沙箱设计
```
