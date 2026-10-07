# chat / summary 迁移到 OpenAI Agents SDK 计划

> 目标版本:`openai-agents` **0.22.3**（源码见 `cache/openai-agents-python`）；沙箱运行时 `mirage-ai` **git main**（monorepo，源码见 `cache/mirage`，依赖需 `#subdirectory=python`）
> 已确认决策：全量迁移（chat 主 agent + summary 总结器 + image_caption 图片转述）；**直接替换**，不做双引擎开关；会话后端用**内置 `SQLiteSession`**；文件能力走 **mirage 沙箱**（进程内虚拟文件系统 + sandlock 约束的宿主进程，**取代 Docker**，见 1.4）；**移除 openai-proxy**，chat 链路直连上游；会话压缩直接使用 **`openai-agents-context-compaction`**（读时滑动窗口，评估结论见 1.2）；上游已换 **deepseek 官方 API**，429 无需特殊处理；**配置文件结构允许重构，无需兼容旧配置**。
>
> **沙箱方案变更（2026-10-03，已实测验证）**：原计划的 Docker 沙箱替换为 mirage。每会话内存从"每容器数 MB~数十 MB"降到 **约 0.1MB**（5 会话实测：140.3MB → 144.7MB），去掉 docker daemon 依赖与容器冷启动。已知代价与缺陷见 1.4 末尾「实测结论与已知限制」。

---

## 0. 迁移总览

### 0.1 现状 → 目标映射

| 能力                              | 现状（github-copilot-sdk 1.0.16）                               | 目标（openai-agents 0.22.3）                                                                                                                |
| --------------------------------- | --------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 模型接入                          | BYOK provider 字典 + `model_dump_session_config()` + wire patch | `AsyncOpenAI(base_url)` + `OpenAIChatCompletionsModel` + `ModelSettings`                                                                    |
| `max_output_tokens`               | openai-proxy `inject_request` 注入                              | `ModelSettings(max_tokens=...)` 直配                                                                                                        |
| `reasoning_effort`                | 会话配置透传                                                    | `ModelSettings(reasoning=Reasoning(effort=...))` → 顶层 `reasoning_effort`                                                                  |
| `max_context_window_tokens`       | wire patch（SDK 不透传）                                        | 无需传运行时；用于应用层历史截断策略                                                                                                        |
| 429 重试                          | openai-proxy `retry`（Retry-After 分钟级退避）                  | 不再需要：上游已换 deepseek 官方 API，openai client 内置重试即可                                                                            |
| 会话历史                          | Copilot 运行时持久化 + `resume_session`                         | `SQLiteSession`（单库多 session_id，`asyncio.to_thread` 包装）                                                                              |
| 会话压缩                          | `session.rpc.history.compact`                                   | `LocalCompactionSession` 读时滑动窗口（直接使用三方库，见 1.2）                                                                             |
| 中断 turn                         | `session.rpc.abort`（防运行时僵尸 turn）                        | `task.cancel()`（进程内，天然干净）                                                                                                         |
| 流式消费                          | 自研跨线程事件桥接 + 相邻事件超时                               | `Runner.run_streamed().stream_events()` + 自研 watchdog                                                                                     |
| 空响应重发（DeepSeek final 通道） | 检测 + `history.truncate` + 重发                                | SDK 已把 `reasoning_content` 转 reasoning item、空 content 不落历史（400 坑消失）；final_output 为空仍需应用层检测 + `pop_item` 回退 + 重发 |
| 附件（图片）                      | `Attachment` TypedDict                                          | user content 内 `input_image`（data URL）                                                                                                   |
| 工具注册                          | `define_tool` + pydantic params                                 | `@function_tool`（或 `agents.decorators.tool`）                                                                                             |
| 动态工具上下文                    | 闭包持有 `SessionInfo`/`bot_id`，每次发送重建工具               | `RunContextWrapper[ChatContext]`，Agent/工具静态化（推荐）                                                                                  |
| 文件工具                          | Copilot 内置 create/edit/view/glob/grep + PathPolicy 审批       | mirage 沙箱：`Shell(exec_command/write_stdin)`；文件编辑走 shell（见 1.4）                                                                  |
| 权限审批                          | `make_fs_permission_handler`（PathPolicy）                      | 删除；边界=sandlock（Landlock/seccomp）+ mirage VFS 作用域；宿主工具自身只碰沙箱工作区                                                      |
| MCP                               | `mcp_servers` YAML → Copilot 运行时管理                         | `MCPServerStreamableHttp`（tavily / anysearch 直接映射）                                                                                    |
| GitHub MCP 注入                   | `disabled_mcp_servers` 规避                                     | 不存在该问题，配置删除                                                                                                                      |
| 主动记忆                          | 自研 SQLite MemoryStore + 3 工具                                | **原样保留**（仅工具注册方式更换；SDK session/sandbox-memory 均不满足作用域隔离需求，维持 MEMORY_DESIGN.md 结论）                           |
| 消息缓冲区/系统通知               | 应用层（deque + JSON 缓存）                                     | 原样保留（与 SDK 无关）                                                                                                                     |

### 0.2 删除清单

- `kanade_bot/utils/copilot.py`（COPILOT_CLIENT、wire patch、abort、事件桥接、空响应重发全套）
- `kanade_bot/plugins/chat/agent/copilot.py` → 重写为 `manager.py`
- `kanade_bot/plugins/chat/agent/permissions.py`（PathPolicy + fs 审批 handler）
- `tool.py` 中 `download_file`、`create_directory`（沙箱内 `curl`/`mkdir` 替代）
- `utils/schema.py` 的 `model_dump_session_config()`、`available_tools`/`excluded_tools`/`disabled_mcp_servers`/`additional_directories` 字段
- 依赖：`github-copilot-sdk`；`tests/copilot_sdk/` 归档
- config.yaml：chat/summary/image_caption 的 provider 全部改直连（去掉 `:39211` 代理端口）；`openai-proxy/` 目录不再被引用（留存与否自行决定）

---

## 1. 分层设计

### 1.1 模型接入层（新 `kanade_bot/utils/agents_runtime.py`）

```
BaseAgentConfig (YAML) ──► get_model(cfg) ──► OpenAIChatCompletionsModel
                              │
                              └─ AsyncOpenAI(base_url, api_key, max_retries)
```

- **模型缓存**：按 `(base_url, api_key, model)` 缓存 `AsyncOpenAI`/model 实例，避免每次 run 重建连接池。
- **ModelSettings 映射**（新 helper，替代 `model_dump_session_config`）：
  - `reasoning_effort` → `Reasoning(effort=...)`（chat completions 路径发顶层 `reasoning_effort`，SDK 已验证 L675-701）
  - `max_output_tokens` → `max_tokens`（proxy inject 的替代）
  - 重试：`AsyncOpenAI` 默认内置重试（429/5xx 短退避）即可——上游已换 deepseek 官方 API，无 sensenova TPM 窗口问题
- **tracing**：`set_tracing_disabled(True)`（非 OpenAI 平台 key）。
- 启动/关闭：无全局客户端生命周期（纯进程内），`drivers.on_startup` 里只做模型实例预热（可选）。

### 1.2 会话管理层（重写 `chat/agent/copilot.py` → `chat/agent/manager.py`）

保留 `CopilotSessionManager` 的全部应用层职责，仅替换底层会话对象：

- **会话对象**：`LocalCompactionSession(SQLiteSession(session_id, db_path=data/chat/agent_sessions.sqlite3), window_size=...)`；`_sessions` 缓存改为缓存 session 实例 + 关联的运行中 task。
- **per-session `asyncio.Lock` + 全局锁**：原样保留（SDK 不提供同 session 串行保证；SQLiteSession 写操作自带 mutation 串行，但"取历史→跑 run→写回"整个 turn 必须原子）。
- **保留原样**：消息缓冲区（deque + JSON 持久化）、`add_system_notification`、`_build_send_prompt`（RAG 文档/缓冲区/引用/发送者信息拼接）、群聊身份切换（`_update_memory_context`，改为更新 ChatContext）。
- **reset_session**：cancel 在跑 task → `session.clear_session()` → 销毁沙箱容器 + 删 snapshot → 清缓冲区。
- **interrupt（SUPERUSER 命令）**：`task.cancel()`。
- **会话压缩（直接使用 [`openai-agents-context-compaction`](https://github.com/damianoneill/openai-agents-context-compaction) v0.3.0）**：
  - `LocalCompactionSession(underlying, window_size=N, token_budget=M)` 包装任意 Session，**读时压缩**——每次 `get_items()` 自动截到窗口内，`function_call`/`function_call_output` 成对原子保留，底层存储**全量保留**（可审计）；每周对最新 SDK 做兼容测试（0.22.3 已验证）；MIT。
  - **评估结论（为什么够用）**：它是丢弃式而非摘要式，但聊天场景的长期信息本就由主动记忆承担，会话历史只需近期上下文；且零 LLM 调用、零摘要质量风险、无 compaction 阻塞流式的问题（对比 SDK 官方 `OpenAIResponsesCompactionSession` 的 auto-compaction 阻塞行为）。
  - **参数**：以 `window_size` 为主（建议 50–100 起步，聊天含工具轮均 4–8 items/轮）；`token_budget` 可选——注意默认 token_counter 是 ~4 字符/token 估算，对中文偏低估，如启用建议配 `TiktokenCounter()`（`[tiktoken]` extra）或自定义 counter，或干脆只用 window_size。
  - **手动 compact（SUPERUSER 命令）语义变化**：压缩已自动化、无需手动触发；命令保留但改为**存储层物理清理**——把底层 SQLiteSession 中窗口外的 items 删除（控制库体积），回报删除条数。
  - **不满足再自研的触发条件**：若后续需要"摘要式保留很早的对话脉络"（窗口外信息模型完全不知道），再考虑自研 LLM 摘要压缩或等该库 roadmap 中的 LLM-based summarization。

### 1.3 Agent 定义与运行层

- **Agent 静态化 + 动态上下文**：
  - 定义 `ChatContext` dataclass：`session_info`、`bot_id`、`memory_context`、`sandbox` 引用等；
  - 工具签名统一 `(ctx: RunContextWrapper[ChatContext], params...)`，从 `ctx.context` 取会话身份——替代现在"每次发送重建闭包工具"的模式，Agent 对象与工具列表可全局单例；
  - 每次 run 前（会话锁内）更新 `ChatContext`（群会话多成员复用时的记忆身份切换语义不变）。
- **系统提示词**：`_build_system_prompt()` 逻辑保留（文件加载 + extras 替换）；每会话动态段（工作目录→沙箱工作区说明、群信息、OS）拼进 instructions 或首条上下文。
- **发送与流式**（`manager.send_and_wait` 生成器接口保持，`chat.py` 消费方式不变）：
  - `Runner.run_streamed(agent, input, session=..., context=..., run_config=...)` 包成 `asyncio.Task`；
  - 消费 `stream_events()`：`raw_response_event`+`ResponseTextDeltaEvent` 用于 watchdog 心跳；**按 message item 聚合产出**（对齐现有"每条 AssistantMessageData 发一条平台消息"的 UX，实施时核对 `stream_events.py` 中 message 级事件的确切类型名）；
  - **watchdog**：相邻事件间隔超时（沿用 600s）→ `task.cancel()` → 抛 `TimeoutError`（进程内取消即中断在途 LLM 请求与工具循环，无 Copilot 时代的僵尸 turn 问题）；
  - 错误：`SessionErrorData` 等价物不存在，run 异常直接抛给上层（chat.py 已有兜底）。
- **空响应处理（DeepSeek final 通道）**：
  - SDK 层已保证：`reasoning_content` → reasoning item，空 `content` 不产生 assistant message item，历史不会再被污染（上游 400 坑消失）；
  - 应用层保留检测：run 正常结束后，本轮无任何非空 assistant message 且无工具调用 → 用 `pop_item()` 回退本轮写入的 items（run 前 `get_items()` 记录长度）→ 重发同一输入，至多 `EMPTY_RESPONSE_MAX_RETRIES=2` 次；耗尽仍空 → 抛 `EmptyResponseError`（保留现有异常类型，上层报错文案不变）。
- **附件**：
  - `utils/parse.py` 解耦 copilot：`Attachment` TypedDict → 中性 `ImageInput`（`path`/`url`/`mime`/`name`）；
  - 发送层转换为 user content：`[{"type":"input_image","image_url":"data:<mime>;base64,..."}]`；
  - `model_capabilities.supports.vision=False` 时走 `image_caption` 兜底（逻辑保留，见 1.7）。

### 1.4 mirage 沙箱层（重写 `chat/agent/sandbox.py`）

**为什么换掉 Docker**：`mirage`（[strukto-ai/mirage](https://github.com/strukto-ai/mirage)）把「文件系统 + shell + 运行时」做进 bot 进程，配合 sandlock（Landlock + seccomp）约束 native 进程，不需要容器。实测每会话内存 **≈0.1MB**（Docker 为数 MB~数十 MB/容器），且无 docker daemon 依赖、无冷启动。

**架构**（每个聊天会话一套）：

```
聊天会话 ── SandboxManager ── MirageSandboxClient ── Workspace
                                                     ├─ /<abs_host_dir> → DiskResource  (MountMode.EXEC)
                                                     └─ runtimes=[SandlockRuntime(python3)]
```

**版本**：`mirage-ai` 走 **git main**（`git+https://github.com/strukto-ai/mirage.git@main#subdirectory=python`，monorepo 需 `#subdirectory=python`）。**不用 PyPI 版**：PyPI `0.0.6`/`0.0.7a2` 落后 main 约 1800 个 commit，有 3 个会直接影响本项目的缺陷（见下方实测表）。

**免 FUSE 的关键设计**：把 `DiskResource` 的**虚拟挂载前缀设为它自己的宿主 `realpath`**：

```python
workspace_dir = (snapshot_root / safe_name(session_id)).resolve()   # 必须 realpath
ws = Workspace(
    {str(workspace_dir): (DiskResource(str(workspace_dir)), MountMode.EXEC)},
    mode=MountMode.EXEC,
    runtimes=[SandlockRuntime(captures=("python3",), config={...})],
)
```

这样虚拟路径 == 真实路径，sandlock 拉起的 native 进程能直接看到真实文件——**不需要 FUSE / fuse3 / mfusepy**（mirage 文档称 "Skip the FUSE"）。配套要求：**目录必须 `os.path.realpath()`**（`/tmp` 是 symlink，不解析会导致写进 workspace overlay 而宿主目录为空）。

**SandboxManager（应用层池）**：

- `dict[chat_session_id → ManagedSandbox(workspace, client, session, last_used)]`；
- `acquire(session_id)`：存在且存活 → 刷新时间戳返回；否则新建 Workspace + client + session；
- **回收策略沿用**：后台 sweeper 空闲 > `idle_timeout_minutes` → `close()`；存活数 > `max_concurrent_sandboxes` → LRU；
- **不再需要快照 tar**：DiskVFS 直接落宿主目录，销毁 workspace 不丢文件 → 删除 `LocalSnapshot` 与 `snapshot_dir` 的 tar 逻辑（`delete_snapshot` 改为删除目录）；
- bot shutdown：全部 `close()`；**不再需要孤儿容器清理**（进程内无残留）；
- `reset_session`：`close()` + 删工作区目录。

**sandlock 配置**（`SandlockRuntime`）：

```python
SandlockRuntime(
    captures=("python3",),          # 只委派 python3，mirage 内置命令仍走 VFS
    config={
        "fs_readable": (str(workspace_dir),),
        "fs_writable": (str(workspace_dir),),
        "max_memory": "512M",
        "env": {"PATH": "/usr/local/bin:/usr/bin:/bin"},
    },
)
```

- **不捕获 `@external`**：让 mirage 内置的 `cat/grep/ls/echo/sed/find/curl` 走 VFS（相对路径正常）；只把 `python3` 委派给宿主 CPython；
- `max_memory` 替代原 `mem_limit`（实测生效：256M 下 1.5GB 分配失败）；
- **CLI 缺失时启动即报错**（不做自动回退），部署步骤写入文档；
- 需要 Linux 6.12+（Landlock ABI v6）；本机 kernel 7.0 已验证可用。

**无需兼容层**：main 已原生修复下述三处路径缺陷，直接用上游 `MirageSandboxClient` / `MirageSandboxSession` 即可，不需要任何子类或 monkeypatch。

**Agent 形态**：

- 保持 `SandboxAgent` + `capabilities=[Shell()]`（`Filesystem` 的 `apply_patch` 是 FREEFORM/grammar，仅 Responses API 支持，Chat Completions 下不可用——沿用现有结论）；
- main 版提供 `MirageCapability`，可把工作区挂载信息写进模型指令（当前提示词已手写工作区根路径，按需再引入）；
- 每次发送 `RunConfig(sandbox=SandboxRunConfig(session=acquire(...)))`。

#### 实测结论与已知限制（2026-10-03，全部经 `.venv` 实跑验证）

| 项           | 结论                                                                                                                                                                                                          |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 每会话内存   | 5 会话并发：baseline 140.3MB → 144.7MB，**≈0.1MB/会话**                                                                                                                                                       |
| `MountMode`  | **必须 `EXEC`**；用 `WRITE` 时 `python3` 报 `not in EXEC mode`（exit 126）                                                                                                                                    |
| git 依赖     | `#subdirectory=python` 必填；安装需代理（`git config --global http.proxy`）                                                                                                                                   |
| 版本差异     | PyPI `0.0.6`/`0.0.7a2` 用 `mirage.resource.disk.DiskResource`、`ws.execute(...)`、`mirage.runtime.python.sandlock`；**main 用 `mirage.vfs.disk.DiskVFS`、`ws.shell(...)`、`mirage.runtime.sandbox.sandlock`** |
| sandlock cwd | **main 已修复**：`os.getcwd()` 返回工作区，相对路径可用 → 原 R8 风险消除，提示词无需强制绝对路径                                                                                                              |
| 隔离有效性   | 未授权路径读写全部被拒（`No such file`）、secret 内容未变、`max_memory` 生效                                                                                                                                  |
| 内置 curl    | `-s`/`-L`/`-o file` 正常；**`-m <秒>`/`-k` 被误当端口 → exit 7**（已知限制，不修）                                                                                                                            |
| 快照 tar     | 不再需要（DiskVFS 落宿主目录）                                                                                                                                                                                |
| 会话隔离     | mirage 会话共享底层 VFS、隔离 shell 状态；**用户间文件隔离靠「每聊天会话一个 Workspace」**                                                                                                                    |

**降级路径**：若 sandlock CLI 缺失或 Landlock 不可用，把 `captures` 置空即退回 mirage 内置 runtime（`python3` 需 `mirage-ai[monty]`，能力有缺口）。

### 1.5 工具层（`tool.py` 改造，`@function_tool`）

| 工具                                                          | 迁移方式                                                                                                                                                                          |
| ------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `list_memes` / `view_image`（看 URL）/ `image_search` / `tts` | 原逻辑保留，`define_tool` → `@function_tool`；动态参数改从 `RunContextWrapper[ChatContext]` 取                                                                                    |
| `save_memory` / `recall_memory` / `forget_memory`             | MemoryStore 不动，注册方式同上；身份取 `ctx.context.memory_context`                                                                                                               |
| `send_file` / `send_image`                                    | **改从沙箱工作区取文件**（沙箱 files API 读出字节）→ 发平台；参数只留沙箱内相对路径                                                                                               |
| `render_html_image`                                           | 宿主侧渲染（playwright，现有 htmlrender）→ 产物**写入沙箱工作区**（files API）→ 返回沙箱内路径，模型再 `send_image` 发送                                                          |
| `download_file` / `create_directory`                          | **删除**（沙箱开放网络，`curl`/`mkdir` 自理）                                                                                                                                     |
| MCP（tavily / anysearch）                                     | `MCPServerStreamableHttp(name, url, headers, tools_filter)`；SDK 自动转 function calling（chat completions 兼容）；连接生命周期挂 manager（run 结束不断开、进程退出统一 cleanup） |

### 1.6 记忆层

`memory.py`（318 行）**零改动**；`MEMORY_DESIGN.md` 仅更新"Copilot 工具"一节的措辞（`define_tool` → `@function_tool`，作用域结论不变）。

**已知问题与后续方向（不属本次计划）**：生产中模型很少主动调用 `save_memory`，主动记忆的召回率不理想。后续可选方向：

- **被动注入**：每次 run 前按相关性预检索记忆并注入上下文（`session_input_callback` / 首条消息前置），不依赖模型主动 `recall_memory`；
- **参考 SDK 两阶段记忆**：借鉴 sandbox `Memory()` 的 Phase 1（会话提取）/ Phase 2（合并固化到 `MEMORY.md`）机制，在轮次结束后用小模型异步提取候选事实，再由用户确认或自动合并进 MemoryStore（保留现有作用域隔离与淘汰策略）。

### 1.7 summary / image_caption

- `Summarizer`：临时 Copilot 会话 → `Runner.run(summary_agent, prompt)`（无 session，一次性）；消息记录 deque/JSON 缓存不动；超时 `asyncio.wait_for(120)`；
- `image_caption.py`：`Runner.run(agent, [user content + input_image])`，超时 180s；
- 两者共用 1.1 模型层；系统提示词文件与配置结构不变。

### 1.8 配置与依赖

配置结构**允许重构、无需兼容旧配置**——借迁移之机把 Copilot 概念残留一并清理，直接设计新结构：

- `pyproject.toml`：`- github-copilot-sdk`、`- openai-agents[docker]`、`+ openai-agents`、`+ mirage-ai`（git main，`#subdirectory=python`）、`+ openai-agents-context-compaction`（可选 `[tiktoken]` extra）；不再需要 `docker-py`；
- `utils/schema.py`：`BaseAgentConfig` 重构（不必保留旧字段名）——
  - 保留：`model`、`provider`（`base_url`/`api_key`）、`reasoning_effort`、`model_capabilities`（`vision` 标志供附件兜底判断；`limits` 供窗口大小参考）、`system_prompt_file`、`mcp_servers`；
  - 删除：`available_tools`/`excluded_tools`/`disabled_mcp_servers`/`additional_directories`、`model_dump_session_config()`；
  - 新增：`to_model_settings()` / `to_model()` helper（供 1.1 模型层使用）；
- `config.yaml` / `config-example.yaml` 重写 chat 段：
  - provider 全部直连（指向 openai-proxy 的锚点如 `op-deepseek-flash` 改 deepseek 官方 API）；
  - 新增 `chat.sandbox` 段：`enabled / max_concurrent_sandboxes / idle_timeout_minutes / sweeper_interval_minutes / workspace_dir / memory_limit / environment`（**删除** `image`/`mem_limit`/`cpus`/`max_concurrent_containers`/`snapshot_dir`，命名改为 `*_sandboxes` 以贴合新语义）；
  - 新增 `chat.session` 段：`db_file / compaction_window_size / compaction_token_budget(可选)`；
  - 删除 `disabled_mcp_servers`、`excluded_tools`、`additional_directories`；
- 重新生成 `schemas/*.json`。

---

## 2. 实施步骤（按可验证的增量划分）

### 阶段一：模型层 + summary（最小闭环）
1. 新建 `utils/agents_runtime.py`（模型缓存、ModelSettings 映射、tracing 禁用）；
2. 重写 `summary/summarizer.py` 为 `Runner.run`；
3. 直连上游验证：`reasoning_effort`/`max_tokens` 到 wire、总结质量；
4. 删除 chat 对 proxy 的引用。
   **验证**：summary 命令端到端 + 抓包确认请求体字段。

### 阶段二：chat 核心（无沙箱）
1. `ChatContext` + Agent 静态化，`tool.py` 中宿主静态工具（memes/view_image/image_search/tts/memory×3）迁 `@function_tool`；
2. 重写 `manager.py`：`LocalCompactionSession(SQLiteSession)` 会话对象、锁、缓冲区、通知、流式 + watchdog + cancel、空响应回退重发；
3. `utils/parse.py` 附件解耦 → `input_image`；`image_caption.py` 迁移；
4. MCP（tavily/anysearch）接入。
   **验证**：多轮对话、重启后会话恢复、reset、缓冲区攒消息、系统通知、图片输入、群身份切换、600s 超时取消、空响应自动重试。
   （此阶段 `send_file/send_image/render_html_image` 暂不可用——依赖阶段三的沙箱。）

### 阶段三：mirage 沙箱
1. `pyproject.toml` 换依赖（去 `[docker]`，加 `mirage-ai` git main）；
2. 重写 `chat/agent/sandbox.py`：`_exec_internal` cwd 兼容层 + `Workspace`/`MirageSandboxClient` 封装 + `SandboxManager`（TTL/LRU/sweeper，DiskVFS 落宿主目录，免 FUSE）；
3. Agent → `SandboxAgent`（维持 `capabilities=[Shell()]`），系统提示词注入工作区绝对路径；
4. `send_file/send_image`（读沙箱）、`render_html_image`（宿主渲染 → 写沙箱）改造；
5. 删除 `download_file/create_directory/permissions.py`。
   **验证**：内存占用对比（预期 ≈0.1MB/会话）；sandlock 隔离（未授权路径读写被拒、`max_memory` 生效）；`python3` 读写工作区文件；render→沙箱→send 全链路；reset 清目录；TTL/LRU 回收后文件仍在。

> **部署前置**：需先安装 `sandlock` CLI 到 `PATH`（Linux 6.12+；本机 kernel 7.0 已验证）。缺失时 bot 启动直接报错，步骤见 README。

### 阶段四：治理与清理
1. compact 存储清理版 SUPERUSER 命令（物理删除窗口外 items，见 1.2）+ `matcher.py`/`handler.py` 适配；
2. 删除 `utils/copilot.py`、`pyproject` 依赖、`tests/copilot_sdk/` 归档、`MEMORY_DESIGN.md` 更新、schema 重新生成；
3. 回归：全部 SUPERUSER 命令、ban、水晶扣减、Console 适配器路径。

---

## 3. 风险与开放问题

| #   | 风险                                                                                                                                           | 缓解                                                                                                                                               |
| --- | ---------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| R1  | **三方 compaction 库较早期**（`openai-agents-context-compaction`：alpha、读时压缩无缓存 O(n)、默认 token 估算对中文偏低估）                    | 实现面小（滑动窗口+调用对原子性），出问题可 vendor 单包自维护；每周 CI 对最新 SDK 兼容验证（0.22.3 已过）；`token_budget` 不准则只用 `window_size` |
| R2  | **SandboxAgent beta**：0.22.3 的 sandbox API（`base_instructions`/capabilities/session state）可能在后续版本变动                               | 沙箱层集中封装在 `sandbox.py` 单文件；升级 SDK 时对照 changelog                                                                                    |
| R3  | **流式"多条消息"语义差异**：copilot 按 AssistantMessageData 分条（工具调用间穿插文本），SDK 按 message item；deepseek-flash 的穿插行为可能不同 | 按 message item 聚合产出，`chat.py` 的分段发送逻辑（按空行/代码块拆）不变，UX 差异实测后微调                                                       |
| R4  | **`pop_item` 回退**与 SDK 写入顺序的耦合（空响应重试用）                                                                                       | 回退长度 = run 前后 `get_items()` 差集，只在会话锁内操作；端到端测试覆盖（可参考 `tests/copilot_sdk/test_final_channel.py` 的 mock 思路重写）      |
| R5  | ~~容器冷启动延迟~~ mirage 为进程内 workspace，**无冷启动**（原 Docker 风险已消除）                                                             | —                                                                                                                                                  |
| R6  | ~~Docker 资源限额注入依赖覆写私有方法~~ 改为 sandlock `max_memory`（实测生效）                                                                 | —                                                                                                                                                  |
| R7  | ~~mirage PyPI 版路径缺陷~~ **已消除**：改用 git main，3 处缺陷均已上游修复                                                                     | 保持 git 依赖；升级时回归 1.4「实测结论」表                                                                                                        |
| R8  | ~~sandlock 子进程 cwd 异常~~ **已消除**：main 已修复，相对路径可用                                                                             | 提示词仅需给出工作区根路径供模型参考                                                                                                               |
| R9  | **sandlock CLI 为外部依赖**，需 Linux 6.12+（Landlock ABI v6）；缺失则沙箱不可用                                                               | 启动时检查 `shutil.which("sandlock")`，缺失直接报错并提示安装步骤（不做静默回退）；降级路径：`captures` 置空 + `mirage-ai[monty]`                  |
| R10 | **内置 curl flag 解析缺陷**：`-m <秒>`/`-k` 被误当端口 → exit 7                                                                                | 已知限制，写入文档；提示词引导用 `-s`/`-L`/`-o <file>`；或将来把 `curl` 纳入 sandlock captures 用真二进制                                          |
| R11 | **git 依赖指向 main（非 tagged release）**：上游变更可能引入不兼容                                                                             | 需代理才能安装（已在 README 写明）；`uv.lock` 锁定具体 commit；升级时跑 `tests/chat/test_mirage_sandbox.py` 回归                                   |
| O1  | `RunItemStreamEvent` 中 message 级事件的确切类型名、`ViewImageTool` 容器内可用性、MCP 断线重连行为                                             | 实施时核对 `src/agents/stream_events.py` / 实测                                                                                                    |

## 4. 明确不做

- 不迁移历史 Copilot 会话（SDK items 格式不兼容，全部会话从零开始；summary 的 JSON 消息记录缓存保留）；
- 不引入 SDK 原生记忆体系（`extensions/memory` 是 session 后端、sandbox `Memory()` 是文件式经验记忆，均无用户/群作用域隔离）；
- 不做双引擎开关（直接替换，出问题回滚 git）。
- **不修复 mirage 内置 curl 的 `-m`/`-k` flag 缺陷**（上游问题，走文档约束）；
- **不引入 FUSE**（用「挂载前缀 = 宿主 realpath」方案免掉 fuse3/mfusepy 依赖）；
- **不追 PyPI 版 mirage**（落后 main 约 1800 commit 且有 3 处路径缺陷）；统一用 git main。
