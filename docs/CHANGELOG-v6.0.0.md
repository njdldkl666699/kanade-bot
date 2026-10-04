# v6.0.0-beta.1 更新日志

> 本版本将 chat 模块与 summary 模块的底层从 `github-copilot-sdk` 整体迁移至
> `openai-agents`（0.22.3），agent 应用层（主动记忆、会话管理、Docker 沙箱、
> 拼接续写）全部基于新 SDK 重建。

## 破坏性变更

- **上游切换**：chat / summary / image_caption 全部改经 OpenAI 兼容 Chat
  Completions 端点直连（当前为 deepseek 官方 API），`openai-proxy` 与
  `github-copilot-sdk` 不再被引用；`max_output_tokens` 由 `ModelSettings`
  直接表达，不再依赖代理注入
- **配置结构重构**（无需兼容旧配置）：
  - `ProviderConfig` 精简为 `base_url/api_key/headers`
  - `BaseAgentConfig` 扁平化：`model/provider/reasoning_effort/max_output_tokens/
    vision/system_prompt_file/mcp_servers`
  - 移除 `available_tools/excluded_tools/disabled_mcp_servers/
    additional_directories` 等 Copilot 概念字段
  - 新增 `chat.session`（SQLite 存储 + 读时压缩窗口）与 `chat.sandbox`
    （Docker 沙箱）配置段
- **会话历史不迁移**：原 Copilot 运行时会话无法转换为 SDK items 格式，所有
  会话从零开始（summary 的消息记录缓存保留）
- **压缩会话命令语义变更**：发送给模型的历史已由滑动窗口自动截断，`/压缩会话`
  改为存储层物理清理（删除窗口外 items 控制库体积）
- **工具变化**：移除 `download_file` / `create_directory`（由沙箱内 shell 承担）；
  `send_file` / `send_image` / `render_html_image` / `view_image` / `image_search`
  以沙箱工作区为中心；权限审批层（PathPolicy）随 Copilot 移除，边界为容器

## 新增

- **Docker 沙箱**（`chat/agent/sandbox.py`）：每聊天会话一个容器，开放网络；
  空闲 30 分钟 TTL 回收、最多 4 容器 LRU 淘汰（销毁前快照保留工作区，下次
  消息自动恢复）；单容器 mem_limit 256m（同时设 memswap 禁用 swap）、CPU 1 核；
  支持环境变量注入（如 PIP_INDEX_URL pip 镜像，直接使用 SDK `Environment`
  模型结构）；启动时按 label 清理上次 crash 遗留的孤儿容器
- **输出截断拼接续写**（全部 assistant 调用，与 Copilot CLI 行为一致）：模型
  输出因 `max_output_tokens` 截断（finish_reason=length）时，回传已生成内容
  并以 "Please continue from where you left off." 续写直至完成；chat 流式
  场景下续写内容继续逐条发送
- **会话压缩**：`openai-agents-context-compaction` 读时滑动窗口（80 items，
  function_call 对原子保留），历史全量存于 SQLite 可审计
- **MCP**：tavily / anysearch 经 `MCPServerStreamableHttp` 接入，startup 阶段
  直连（消除启动后首条消息缺工具的竞态）

## 改进

- 空响应（DeepSeek 推理通道整轮无输出）重发逻辑保留，且 SDK 原生将
  `reasoning_content` 转为 reasoning item、空 content 不落历史——原上游 400
  报错路径消失
- 超时取消从 RPC abort 改为进程内 `task.cancel()`，天然中断在途请求与工具
  循环，无僵尸 turn
- 主动记忆（SQLite MemoryStore + save/recall/forget 三工具）与消息缓冲区、
  系统通知、群聊身份切换等应用层逻辑原样保留
- 代码净减约 2200 行（移除自建跨线程事件桥接、wire patch、权限审批等）

## 已知问题

- nonebot 插件加载存在偶发的顺序竞态（htmlrender 相关，重跑即过）
- chatrecorder 插件 Console 适配器的 CalledAPI hook 已在本地修复（场景解析
  对齐 uninfo、消息字段 content），待提交至上游 fork

---

# v6.0.0-beta.2 更新日志

> 本版本将 chat 的文件与 shell 能力从 **Docker 容器**换成
> [mirage](https://github.com/strukto-ai/mirage) 进程内沙箱（sandlock 约束），
> 并完成向 Pydantic AI 的迁移方案评估（计划已定，尚未实施）。

## 破坏性变更

- **沙箱后端替换**：Docker 容器 → mirage 虚拟文件系统 + sandlock 约束。
  工作区从容器内真实文件系统改为宿主目录上的 mirage 虚拟文件系统，
  **行为等价但隔离机制与部署要求完全不同**
- **不再依赖 Docker**：`openai-agents[docker]` extra 移除（不再拉 `docker-py`），
  无需 docker daemon；新增 `mirage-ai` git 依赖
- **`chat.sandbox` 配置段字段重命名**（结构不兼容旧配置）：
  - 删除 `image`（不再需要容器镜像）、`mem_limit`、`cpus`、`snapshot_dir`
  - `max_concurrent_containers` → `max_concurrent_sandboxes`
  - `snapshot_dir` → `workspace_dir`（语义从「快照目录」变为「工作区根目录」）
  - `mem_limit`（容器内存）→ `memory_limit`（sandlock 子进程内存，默认 512M）
- **工作区不再需要快照**：DiskVFS 直接落宿主目录，沙箱回收/重建后文件自然保留，
  原有 tar 快照机制与孤儿容器清理逻辑移除
- **沙箱启用前置依赖**：需安装 `sandlock` CLI，缺失时 bot **启动即报错**
  （不静默降级）；临时不用可将 `chat.sandbox.enabled` 设为 `false`
- **沙箱工作目录路径规范**：每会话工作区位于 `sandboxes/<会话ID>/`，路径经
  `realpath()` 解析（`/tmp` 常为 symlink，不解析会导致写入落在 workspace
  overlay 而宿主目录为空）

## 新增

- **mirage 沙箱**（`chat/agent/sandbox.py` 重写）：每聊天会话一个独立 Workspace，
  虚拟挂载前缀设为工作区宿主 realpath 实现**免 FUSE**（无需 fuse3/mfusepy）；
  `python3` 经 sandlock（Landlock + seccomp）约束，单进程内存上限
  `memory_limit`；TTL + LRU 池化逻辑沿用
- **沙箱隔离测试**（`tests/chat/test_mirage_sandbox.py`，23 项）：覆盖 cwd 语义、
  免 FUSE 落盘、sandlock 隔离边界（未授权路径读写被拒、secret 内容不变、
  内存限额生效）、TTL/LRU 回收、每会话内存占用、会话 ID 路径逃逸防护
- **Pydantic AI 迁移方案**（`MIGRATION_PLAN_PYDANTIC_AI.md` + 执行提示词
  `PYDANTIC_AI_MIGRATION_PROMPT.md`）：含 5 个实施阶段、API 映射表、风险清单与
  已定决策；含官方迁移技能包指引、bubblewrap 对照实测、压缩事件落库设计

## 改进

- **内存占用大幅降低**：沙箱从「每会话一个容器」变为进程内虚拟文件系统，
  实测**每会话约 0.1MB**（5 会话并发：140.3MB → 144.7MB；Docker 为每容器
  数 MB 至数十 MB）
- **无容器冷启动**：沙箱创建为进程内操作，消除 Docker 容器启动延迟
- **隔离语义更贴合本项目**：工作区外路径在 mirage VFS 中**根本不存在**，
  模型读不到 bot 自身的 `config.yaml` / 源码 / `.env`（对照：bubblewrap 的
  `--ro-bind / /` 会让这些可读，故未采用）
- **依赖简化**：不再需要 docker daemon 与 `docker-py`

## 已知问题

- **生产内核 6.8 的 Landlock 仅 ABI v4**，而 sandlock 要求 ABI v6（Linux 6.12+）。
  sandlock 默认 Strict 会在 protection 不可用时**拒绝启动** → 需显式降级
  （wrapper 注入 `--allow-degraded`）。降级后失去 SignalScope /
  AbstractUnixSocketScope / FsIoctlDev 三项保护；**文件系统隔离主线不受影响**
  （依赖 Landlock FS rules + mirage VFS，非 v6 特性）。详见
  `MIGRATION_PLAN_PYDANTIC_AI.md` 4.3.0.1
- **mirage 依赖 git main 分支**：PyPI 版（0.0.7a2）落后约 1800 个 commit，
  存在工作目录不传 cwd、read/write 不解析相对路径、sandlock 子进程 cwd 错误
  三处缺陷；且安装需能访问 GitHub（`#subdirectory=python` 必需）
- **mirage 内置 `curl` 参数缺陷**：`-m`（超时）与 `-k`（忽略证书）会被误解析为
  端口，返回退出码 7；请使用 `-s` / `-L` / `-o <文件>`
- **沙箱挂载模式必须为 EXEC**：使用 `WRITE` 时 `python3` 会以
  `not in EXEC mode` 失败（exit 126）

---

# v6.0.0-beta.3 更新日志

> 本版本完成 chat / summary / image_caption 从 `openai-agents` 到
> **Pydantic AI**（2.54）的整体迁移，并重写会话压缩与历史存储：
> 滑动窗口 → **零成本工具结果清理 + 压缩事件落库**，历史改为 append-only
> 全量保留、恢复时按事件重放。

## 破坏性变更

- **底层框架切换**：`openai-agents` / `openai-agents-context-compaction` 移除，
  改为 `pydantic-ai-slim[mcp,openai]` + `pydantic-ai-harness`。chat / summary /
  image_caption 全部改经 Pydantic AI 的 OpenAI Chat Completions 端点
- **不再需要自建压缩**：滑动窗口读时截断（会每轮移动前缀起点、彻底打掉 provider
  前缀缓存）移除，改用官方 `ClearToolResults` —— 就地清空旧工具**结果**的内容，
  **消息条数与位置不变**，前缀稳定
- **会话存储语义变更**：`messages` 表 **append-only 全量保留**（只追加，永不修改、
  永不删除）；`/压缩会话` 命令**不再删除任何消息**，改为报告统计（全量条数 /
  压缩后实际发送条数）
- **压缩从会话存储配置中独立**：新增 `chat.compaction` 配置段；
  `chat.session` 语义收窄为「消息缓冲区 + 数据库」；持久化记忆独立为 `chat.memory`
- **配置结构重构**（无需兼容旧配置）：
  - `chat.compaction`：`trigger_fraction` / `keep_pairs` / `min_clear_tokens` /
    `context_window` / `summary_target_fraction` / `summary_model` /
    `summary_keep_messages`
  - `chat.session`：`db_file` / `buffer_max_size` / `buffer_cache_file`
    （旧 `session_messages_max_size` / `session_messages_cache_file` 移除）
  - `chat.memory`：`database_file` / `max_records_per_scope`
    （旧 `memory_database_file` / `memory_max_records_per_scope` 移除）
- **沙箱降级策略自动化**：生产内核 6.8 的 Landlock 仅 ABI v4（sandlock 要求 v6），
  beta.2 需手动处理；本版本按 `chat.sandbox.landlock_degrade` 自动决策
  （`auto` 默认 / `always` / `strict`），`auto` 下自动生成 wrapper 注入
  `--allow-degraded`。ABI < v4 时**直接报错**（连文件系统规则都保不住，
  不容降级）
- **工具层重写**：宿主工具从 `FunctionTool` 装饰器改为普通可调用对象 +
  `PrepareTools` 能力按运行期状态过滤（关闭的工具直接从 schema 移除）；
  文件与命令能力改走官方 `Shell` / `FileSystem` 能力 + `ctx.workspace`
- **模块移动**：`utils/agents_runtime.py` → `utils/pai_runtime.py`；
  `agent/context.py` → `agent/deps.py`

## 新增

- **压缩事件落库与自验证**（`agent/compaction.py`）：每轮结束时对落库的
  `CompactionMark` 做一次重放自验证 —— 能复现就只存参数（零额外存储），
  否则判定触发了 LLM 摘要档并存完整快照。**「能不能重放」不靠猜，每轮实测**
- **分层压缩（默认不启用）**：配置 `compaction.summary_target_fraction` 后，
  `TieredCompaction` 先跑零成本清理，仍超预算才升级到 `SummarizingCompaction`
  （可指定 `summary_model` / `summary_keep_messages`）。未配置时**只用零成本档**，
  压缩永远可重放、不调用 LLM
- **恢复一致性保证**：在线压缩与恢复重放**共用同一个 `apply_strategy`**
  （本设计最重要的不变式，两套代码必然漂移，一漂移前缀就变、缓存就失效）；
  参数漂移时自动丢弃旧 marks 并从全量重放
- **真正的文件编辑工具**：`edit_file` 在 Chat Completions 下可用
  （`openai-agents` 的 `apply_patch` 是 FREEFORM 工具，仅 Responses API 支持，
  此前只能靠 shell 的 sed/cat 变相编辑）
- **Landlock ABI 自动检测与降级**（`agent/sandbox.py`）：`check_sandlock()` 探测
  ABI，按策略生成降级 wrapper（`sub=$1; shift` 而非 `${@:2}` —— 后者是 bash
  扩展，dash 下会静默失效导致「看似运行、实际未降级」）；降级代价见「已知问题」
- **`MirageBackend` 完整实现 `WorkspaceBackend` 协议**：read / write / edit /
  execute / exists / glob / grep / ls / mkdir / remove / stat 全部覆盖，
  路径统一经 `realpath` 解析并做逃逸防护
- **Pydantic AI 端到端测试**（`tests/chat/test_pai_agent.py`，15 项）：覆盖压缩
  确定性 / 幂等 / 工具配对保持、append-only 存储、恢复一致性（逐字节比对）、
  参数漂移、流式事件分流与中断
- **沙箱后端测试**（`tests/chat/test_mirage_backend.py`）：MirageBackend 协议
  实现与 Landlock 降级行为

## 改进

- **前缀缓存友好**：压缩不再移动消息位置，provider 侧 prompt cache 持续命中
  （滑动窗口方案下每轮 prefill 整个上下文）
- **截断续写改用原生字段**：`finish_reason == 'length'` 直接判定
  （`openai-agents` 需自建 `get_length_tracked_model()` 跟踪）
- **流式走完整 agent 循环**：改用 `run_stream_events()` 而非 `run_stream()`，
  工具调用后会继续生成，不再跳过工具调用
- **watchdog 超时更彻底**：超时后 `stream.cancel()` 进程内取消，真正中断在途
  LLM 请求与工具循环
- **`reasoning_content` 处理更干净**：DeepSeek 的推理内容由 Pydantic AI 转成
  `ThinkingPart`，**不会混进最终文本**，省去手写映射
- **空响应重发保留**：整轮无文本且无工具调用时回退本轮消息并重发
  （1+2 次），避免无效轮次残留
- **模型请求上限放宽**：`UsageLimits(request_limit=100)`（默认 50对本项目长工具链
  偏紧会误伤）
- **依赖树更小**：移除 agents SDK 与自研压缩库，换官方 harness

## 已知问题

- **Landlock 降级后失去三项 protection**：生产内核 6.8 的 Landlock 仅 ABI v4
  （sandlock 要求 v6），`auto` 策略下自动注入 `--allow-degraded`，代价是失去
  SignalScope / AbstractUnixSocketScope / FsIoctlDev。**文件系统隔离主线不受影响**
  （依赖 Landlock FS rules + mirage VFS，非 v6 特性）；需完整保护请用 ABI ≥ v6
  的内核（Linux 6.12+）并设 `landlock_degrade: strict`
- **mirage 依赖 git main 分支**：PyPI 版（0.0.7a2）落后约 1800 个 commit，
  存在工作目录不传 cwd、read/write 不解析相对路径、sandlock 子进程 cwd 错误
  三处缺陷；且安装需能访问 GitHub（`#subdirectory=python` 必需）
- **mirage 内置 `curl` 参数缺陷**：`-m`（超时）与 `-k`（忽略证书）会被误解析为
  端口，返回退出码 7；请使用 `-s` / `-L` / `-o <文件>`
- **沙箱挂载模式必须为 EXEC**：使用 `WRITE` 时 `python3` 会以
  `not in EXEC mode` 失败（exit 126）
- **生产库首启会重算一次历史**：已有 `compaction_marks` 存的是压缩参数变更前的
  键，指纹对不上 → 按设计丢弃旧 marks 并从全量重放（宁可损失一次缓存）。
  仅首次启动发生，之后参数稳定即命中

## 迁移实现要点（已在代码中解决，记录以免回退）

以下是迁移过程中踩到并已修复的坑，代码中已正确处理，此处仅作记录：

- **`pydantic-ai-slim` 的 `[mcp]` extra 不含 openai SDK**：`pyproject.toml` 已显式
  写为 `[mcp,openai]`。注意此后若用 `uv remove` 清理其他依赖，可能连带删掉 openai，
  导致 `utils/schema.py` 的 `from openai.types import ReasoningEffort` 崩溃
- **`Agent.toolsets` 是只读 property**（`fset is None`）：MCP 连接发生在 Agent
  构造之后的 on_startup，无法再挂到 Agent 上。改为每次运行通过
  `run_stream_events(toolsets=...)` 的 per-run 通道传入
  （`PrepareTools` 只作用于 tool_defs，改动不影响工具过滤语义）
- **`finish_reason` 只存在于 `ModelResponse`**：`ModelMessage` 是
  `Annotated[Union[ModelRequest, ModelResponse], Discriminator('kind')]`，
  判定截断须先 `isinstance(run[-1], ModelResponse)` 收窄。注意
  `ModelResponse.state` 是生命周期状态（complete/incomplete/suspended/
  interrupted），**不是**截断标志
- **`SummarizingCompaction` 构造要求 `max_messages` / `max_tokens` /
  `max_fraction` 三选一必填**：只传 `keep_messages` 会直接 `ValueError`。
  `TieredCompaction` 会绕过 tier 自身触发条件、直接驱动 `compact()`，
  故给 `max_fraction=summary_target_fraction` 即可
- **`CompactionStrategy` Protocol 只有 `compact()`**，没有 `before_model_request`，
  不能用作 capability 包装字段的类型；`ClearToolResults` / `TieredCompaction` /
  `SummarizingCompaction` 的共同基类是 `AbstractCapability`（`RecordingCompaction`
  包裹的字段类型标 `AbstractCapability[AgentDepsT]`，标成 `TieredCompaction`
  会因 `build_strategy()` 可能只返回 `ClearToolResults` 而类型报错）
- **Landlock 降级 wrapper 的 shell 兼容**：必须用 `sub="$1"; shift` 而非
  `${@:2}` —— 后者是 bash 扩展，`/bin/sh`(dash) 不支持，会静默失效
  （沙箱看似运行、实际未降级）
- **测试需绕过 nonebot 插件运行时**：`tests/chat/test_pai_agent.py` 已加
  `nonebot.init()`，并给 `kanade_bot.plugins.chat` 注册一个只有 `__path__` 的
  空壳模块绕过 `__init__.py`（它会连带拉起 handler → `get_driver()`）。
  另需 `PluginManager` 正式加载插件才能满足 localstore 的
  "Cannot detect caller plugin"（它靠栈帧回溯 `__nonebot_plugin__`），
  故采用空壳模块方案
