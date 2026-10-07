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

---

# v6.0.0-beta.4 更新日志

> 本版本分两部分：
> - `0ac2d76`将系统提示词重构为模块化配置——片段文件 + 变量替换，按变化频率分层以命中 provider 前缀缓存；
> - `72f86b2`接入 mirage 官方 Pydantic AI backend 与 `pydantic-ai-backend`的 Console 工具集，删除自写的 `WorkspaceBackend` 协议适配层，并合并压缩参数 / 提示词配置模型。

## 第一部分：系统提示词模块化 `0ac2d76`

### 破坏性变更

- **`BaseAgentConfig` 移除 `system_prompt_file`**：系统提示词不再是单个
  整块文件，改为各子类自行声明；chat 换用新的 `prompt` 配置段
  （`files` / `sections` / `fallback` / `vars`）
- **提示词文件迁移**：`Kanade-wiki.md` 迁为 `config/chat/prompts/wiki.md`；
  `Kanade-v1~v4.md` 保留为历史文件，不再被加载

### 新增

- **模块化系统提示词**（`agent/prompt.py` + `config/chat/prompts/` 目录）：
  原先一整块写死的字符串（Kanade-v4.md）+ `system_prompt_extras` 占位符
  替换 + 沙箱/群聊说明硬编码在动态指令里按条件拼接，拆为 9 个静态片段
  （identity / chat_style / lore_qa / tool_usage / tools /
  system_notifications / wiki / abbreviations / environment）+ 2 个条件
  片段（sandbox / group_chat），可单独调整、可复用
- **按变化频率分两层**：
  - 静态层（`prompt.files`）：进程恒定的内容，构造时求值一次，渲染为多个
    具名 `InstructionPart(dynamic=False)`，id 形如 `agent:identity`
  - 会话层（`prompt.sections`）：每会话恒定的内容（群名、沙箱工作区），
    每轮渲染为单个 `dynamic=True` 的 part，`when` 变量为假时整段跳过
- **内置注入变量**：环境信息、会话信息、沙箱、功能开关；`prompt.vars`
  可注入自定义常量并覆盖内置变量（优先级最高）
- **模板只替换已知变量**：未知占位符原样保留并记日志——`{{happy}}`、
  `{{表情包名称}}` 是表情包引用的活约定，必须原样发给模型（正则仅匹配
  ASCII 标识符，中文占位符天然不命中）

### 改进

- **前缀缓存友好**：分层的动机不是渲染开销（19KB 正则替换一轮 ~100µs，
  可忽略），而是 provider 前缀缓存——系统提示词位于消息列表最前，任何
  一个字节变化都会让后面整块失效。当前时间从系统提示词移出，改由
  `current_time_line()` 附到用户消息（时间每分钟变一次，放前缀里等于每轮
  必崩缓存）；发送者信息同理（已由 `_build_send_prompt` 写入用户消息，
  提示词里的 `{{user_info}}` 属重复）
- 片段间连接交给 `InstructionPart.join`，不再手动拼分隔线
- 验证：同一会话连续两轮 instructions 逐字节一致；19084 字符中静态层占
  19040

## 第二部分：官方工作区后端与 Console 工具集 `72f86b2`

### 破坏性变更

- **沙箱工具集替换**：`pydantic-ai-harness` 的 `Shell` + `FileSystem`
  能力改为新依赖 `pydantic-ai-backend`（≥0.2.16）的 `ConsoleCapability`，
  模型可见的沙箱工具变为标准 Console 工具集：`execute` / `read_file` /
  `write_file` / `edit_file` / `grep` / `glob` / `ls`（工具名变化，沙箱
  提示词片段已同步更新）；权限规则用 `PERMISSIVE_RULESET`——隔离边界由
  mirage VFS + Landlock 提供，不在工具层再拦一道
- **运行期工具过滤移除**：`PrepareTools` capability 删除，工具回到静态
  列表，不再按运行期状态（记忆作用域、TTS 配置）从 schema 中隐藏
- **自写 `MirageBackend` 删除（约 370 行）**：改为 mirage 官方
  `PydanticAIWorkspace` 的子类 `KanadeWorkspace`，只保留必要修正（见
  「新增」）
- **workspace 注入方式变化**：`run_stream_events(workspace=...)` per-run
  参数移除，沙箱 backend 改经 `ChatDeps.backend` 依赖注入
- **`CompactionParams` 数据类移除**：与 `CompactionConfig`（pydantic 配置
  模型）合一；mark 参数序列化直接用 `model_dump()` / `model_validate()`，
  参数指纹改为 `model_dump_json()`
- **`prompt_config.py` 删除**：`ChatPromptConfig` 并入 `chat/config.py`
- **单轮模型请求数上限从 100 放宽为不限制**（`REQUEST_LIMIT = None`）

### 新增

- **官方 backend 的三处必要修正**（`KanadeWorkspace`）：
  1. **相对路径绑定工作区根**：官方按虚拟绝对路径寻址，本项目「免 FUSE」
     布局下相对路径会落到 VFS 根 `/`（宿主系统视图 overlay）——读抛
     FileNotFoundError、写**静默落空**（宿主工作区无文件）
  2. **`execute` 超时**：官方接受 `timeout` 参数但不生效，子类以
     `asyncio.timeout` 真正取消整个 shell 协程
  3. **`grep` / `glob` 默认搜索根**从 VFS 根 `/` 改为工作区根
- **沙箱会话初始化**：在持久 mirage session 上执行一次
  `cd <工作区根>; export K=V`（变量名正则校验防 shell 注入），之后的
  `execute` 天然继承 cwd 与环境变量（原先靠每次 shell 调用传
  `cwd` / `env`）
- **测试重写**（`tests/chat/test_mirage_backend.py`）：官方 backend 行为
  契约测试——相对路径写入落宿主（子进程验证）、execute 的 cwd 与环境
  注入、超时取消、grep/glob 默认根、绝对虚拟路径不受绑定影响、非零退出
  是正常结果、`SandboxSession.write` 自动建父目录且可覆盖（官方 `awrite`
  拒绝覆盖）、`delete_workspace` 在挂载存活时也真删宿主目录、`..` 越界
  失败、`KanadeWorkspace` 必须是官方子类（防止回退成重写协议）
- **测试引导适配**（`tests/chat/test_pai_agent.py`）：`session_store` /
  `compaction` 传递导入 `chat/config.py` 后（beta.3 测试从未导入过它），
  壳模块方案需再补三件事——`nonebot.load_plugin()` 正式加载
  `model_updater`（`require` 需要，且 localstore 靠正式插件的栈帧回溯）、
  把 localstore 调用方探测固定为该插件（壳模块探测不到）、`LOCALSTORE_*_DIR`
  指向临时目录（config 模块级初始化会写出默认 `chat_configs.json`）

### 改进

- **`send_file` 免临时文件**：沙箱工作区本就落在宿主目录（DiskVFS），直接
  用宿主路径上传 OneBot 文件，去掉「临时目录落盘 → 上传 → 删除」三步，
  并加路径越界校验
- **`image_search` 复用全局 `BaiDu` 客户端**：原先每次搜索新建实例
- **`view_image` 分支修正**：不支持视觉且未配置转述模型时返回明确提示
- **代码净减约 450 行**（+864 / −1313）：beta.3 迁移期写下的设计性大段
  docstring 全面精简（设计记录由 `docs/` 承载，代码只留短注释）；清理
  `memory.py` 等处残留的 Copilot 字样；版本号升至 `v6.0.0-beta.4`

### 已知问题

- `/压缩会话` 计划改为手动触发一次 LLM 会话压缩（代码已留 TODO，当前仍
  为统计报告）

---

# v6.0.0-beta.5 更新日志

> 本版本移除沙箱池化层：实测每轮新建沙箱的额外开销仅 ~5ms，TTL/LRU/
> sweeper 的复杂度不再划算；同时修复 sandlock ≥ 0.8.9 下降级自检探针
> 必挂、bot 无法启动的问题。另有四项独立变更：接入 githubcard 插件、
> 十连抽卡渲染增加缩略图磁盘缓存、启动横幅可配置、修复
> `max_output_tokens` 因端点只认 `max_tokens` 而静默失效。

## 破坏性变更

- **沙箱去池化**（`chat/agent/sandbox.py`，净删约 110 行）：
  - 每轮对话新建沙箱、本轮结束即关闭；工作区文件落宿主真实目录，
    跨轮保留语义不变，清空会话仍删除目录
  - 代价：跨轮 shell 状态（`cd`/`export`/后台进程）不再保留——每轮
    初始化本就会重置到工作区根 + 配置环境变量，对模型无感
  - 配置项移除：`chat.sandbox.max_concurrent_sandboxes` /
    `idle_timeout_minutes` / `sweeper_interval_minutes`
  - API：`SandboxManager.acquire/destroy/destroy_all` 移除，改为
    `create(session_id)` + `SandboxSession.close()`
- **实测依据**（mirage clone main + sandlock 0.8.9）：每轮新建+关闭
  ~5ms（Workspace 构造 1.5ms + init shell 2-3ms + close 0.4ms）；
  python3 命令本身的 sandlock 子进程启动 ~33ms 每条命令都要付，池化
  省不掉；每 workspace 常驻内存 ~0.1MB

## 新增

- **接入 nonebot-plugin-githubcard**（`>=0.4.1`）：聊天中的 GitHub
  链接自动以卡片形式回复；schema 生成注册 `GithubCardConfig`，示例
  配置新增顶层 `github_token` / `github_type`，`watchdog.github_token`
  改以 YAML 锚点复用同一 token（一处配置两处生效）
- **启动横幅开关**（顶层配置 `print_kanade_banner` /
  `print_pydantic_ai_banner`，默认均开启）：Kanade 横幅从 `__main__`
  无条件打印移入 `init_nonebot` 按配置打印（`bot.py` 模块顶部的
  banner 导入一并去除）；关闭 Pydantic AI 首次运行 Agent 时的横幅经
  `PYDANTIC_AI_NO_BANNER=1` 环境变量实现

## 改进

- **十连抽卡渲染提速**（`crystal/plugins/gacha/gacha.py`）：新增
  `render_composed_card_thumbnail` 缩略图磁盘缓存，与全尺寸渲染缓存
  同目录、文件名含尺寸（`GACHA_THUMBNAIL_SIZE` 变更后旧缓存自动
  失效）；此前每次十连的每张卡都要解码 940x530 全尺寸图并重新
  LANCZOS 缩放，缓存命中后直接读小图。附验证脚本
  `tests/gacha_thumbnail_cache_test.py`（缓存命中与直接缩放逐像素
  一致、提速效果）与基准 `tests/gacha_render_bench.py`（渲染各阶段
  耗时拆解）

## 修复

- **sandlock ≥ 0.8.9 降级自检必挂**：`--allow-degraded` 变为必须带值，
  原探针（裸 flag + exec `true`）clap 直接报错 → `LandlockUnavailableError`
  → bot 启动失败。探针改为 `-r <python3 顶层目录> -- python3 -c "print(1)"`
  （实测 `-r /usr/bin` 粒度不够，execvp 仍 Permission denied，需放行到
  `/usr` 一级）
- **`max_output_tokens` 从未生效**（`c9c781a`）：pydantic-ai 默认把
  上限映射为 `max_completion_tokens` 发送，而大部分 OpenAI 兼容端点
  只认旧的 `max_tokens`，未知字段被静默忽略（不报错），配置的
  上限实际从未起过作用。`ProviderConfig` 新增
  `supports_max_completion_tokens`（默认 `false`），`get_model` 按端点
  能力注入 `OpenAIModelProfile` 覆盖字段映射；未配置 provider 时保持
  pydantic-ai 默认。附请求捕获脚本 `tests/chat/capture_chat_request.py`
  （经 openai-proxy 抓取一次完整上游请求体，用于验证实际发送字段）

---

# v6.0.0-beta.6 更新日志

> 本版本实现聊天与总结的**按 Token 计费**（峰谷费率、按量结算）；运行时从
> OpenAI 兼容端点 `/models` 自动获取上下文窗口与最大输出；`/压缩会话` 从
> 统计报告升级为手动触发 LLM 摘要压缩（beta.4 遗留 TODO），并新增
> `/会话统计` 命令；沙箱从「每轮新建即弃」改回**会话常驻**。

## 破坏性变更

- **聊天/总结水晶消耗改为按量计费**（`config/crystal/crystal_config.json`
  中「聊天」「总结」的值从固定数字改为 `"按Token计费"` 文字描述）：
  - 使用门槛从「余额 ≥ 固定消耗」变为**余额 > 0**（按量功能的预检查在
    crystal 层只看正余额），实际消耗由插件在轮次/总结结束后按真实 usage
    结算
  - 新增 `consume_crystal()`：按量扣减**允许扣至负数**，不做预检查，
    计费时机与金额由调用方决定；固定扣减路径（`succeed_consume` 等）
    遇到文字描述配置直接 assert 报错
  - chat 不再在首条回复到达时固定扣水晶，改为轮次正常结束（有文本产出
    且非主动回复）时经 `on_usage` 回调结算；总结在成功后按 usage 结算
- **沙箱改回会话常驻**（`chat/agent/sandbox.py`）：`SandboxManager` 为
  每个会话维护常驻沙箱（首次使用创建、跨轮保留），shell 会话状态
  （`cd` / `export` / 后台进程）跨轮不丢；会话重置或进程退出时统一关闭
  （`_shutdown` 调 `close_all`）。beta.5 的「每轮新建即弃」被取代，
  每轮 `create()` 变为幂等获取
- **`/压缩会话` 语义变更**：不再是存储统计报告（该职能移交
  `/会话统计`），而是**不论是否达到自动触发阈值，立即把早期历史压缩为
  一条 LLM 摘要**；会话忙时（3s 内拿不到会话锁）直接失败并提示可先
  `/中断会话`
- `BaseAgentConfig.model` 类型从 `str | None` 收紧为 `str`（默认空串，
  未配置时 `get_model` 照旧报错）

## 新增

- **按 Token 计费**（`utils/billing.py`，新依赖 `chinesecalendar`）：
  - 峰谷判定 `is_peak_hours`：工作日（排除法定节假日，chinesecalendar
    数据不支持该年份或依赖缺失时退化为仅按周末）的 9:00-12:00、
    14:00-18:00 为高峰；周末与节假日全天空闲
  - 计费规则 `compute_token_cost`：输入只按**缓存未命中部分**计费
    （`input_tokens - cache_read_tokens`），输出全量（含 reasoning）；
    向上取整（先 round 截断浮点噪声再 ceil，防整数值因 1e-15 误差多进位），
    单轮不低于 `min_cost`
  - usage 口径：一轮内**全部补全请求累计**（含空响应重发与 length 续写），
    由 manager 的 `on_usage` 回调与 `run_with_continuation(usage=...)`
    原地累加；峰谷按**本轮用户消息时间**判定，不由结算时刻漂移
  - 费率经 `chat.billing` / `summary.billing` 配置（默认高峰输入 12 /
    输出 48、空闲 6 / 24 水晶每千 token，最低 1）
- **模型元数据自动获取**（`utils/pai_runtime.py`）：未显式配置时从
  OpenAI 兼容端点 `/models` 响应提取 `context_window` 与
  `max_output_tokens`（兼容 DeepSeek 风格 `context_window` /
  `max_output_tokens` 与 OpenRouter 风格 `context_length` /
  `max_output_length`），结果按 `(client, model)` 缓存、失败静默；
  `context_window` 优先级为**显式配置 > /models 端点 > genai-prices
  快照回填**——仅在拿到值时才传 profile 字段，显式传 `None` 会因
  pydantic fields_set 语义阻止快照回填；`BaseAgentConfig` 新增
  `context_window` 配置项
- **`/会话统计` 命令**（SUPERUSER，别名 `chat_stats` / `chatstats`）：
  报告当前模型与窗口上限、上下文 token 估算（含窗口占比）、消息条数
  （DB 全量 vs 压缩后实际发送）、沙箱工作区文件列表（限深 3 层、
  最多 10 条，超出省略）
- **crystal 新导出 API**：`consume_crystal`（按量扣减）与 `get_crystal`
  （查余额）
- **`get_config()`**（`scripts/util.py`）：从 NoneBot 全局配置统一提取
  插件配置（env 文件、嵌套分隔符语义与 NoneBot `get_plugin_config`
  一致）；`bot.py` 与 `scripts/github_watchdog.py` 复用，watchdog 里
  手抄的同名实现（约 25 行）删除

## 改进

- **手动压缩与在线压缩共用构造**：`build_summary` 统一构造摘要档
  `SummarizingCompaction`（未启用摘要档时以 1.0 占位 max_fraction），
  `build_summary_mark` 统一落 mark（摘要为 LLM 非确定性产物，无法重放，
  完整快照存入 `result`）；在线 `TieredCompaction` 与手动 `compact_now`
  走同一路径
- **命令别名补全语序**：`重置会话`/`会话重置`、`中断会话`/`会话中断`、
  `压缩会话`/`会话压缩` 两种语序均可触发
- **压缩配置放宽**：`keep_pairs` / `summary_keep_messages` 允许 0
  （`NonNegativeInt`）；`min_clear_tokens` 允许 `None`（不启用最小
  清理阈值）
- **空响应标记修正**：chat 流式消费中 `replied` 改为收到**非空**内容才
  置位（原先首条 part 到达即置位，与「没有收到任何回复」的误报判定
  语义不符）
- ruff 全局忽略 `BLE001`，清理全仓 20 处行内 `# noqa: BLE001`
- README 精简约 90 行：沙箱 Landlock 降级细节、会话历史/压缩/存储设计
  段落移除（设计记录由 `docs/` 与代码注释承载），watchdog 配置说明收敛
- `config-example.yaml` 同步：示例模型换 `deepseek-flash`、新增
  `billing` 锚点段与 `print_*_banner`、prompt 段落改引用默认值
- **测试**（4 个新文件 + 扩充）：`tests/test_billing.py`（峰谷边界
  含左闭右开、午休/周末/国庆、费率计算含缓存命中与最低消耗）；
  `tests/crystal/test_consume_crystal.py`（允许负数余额、预检查门槛）；
  `tests/chat/test_pai_runtime.py`（/models 字段提取两风格、profile
  填充与快照回填不被显式 None 阻断）；`test_pai_agent.py` 新增
  ManualCompactionTest（手动压缩 mark 落库 → 重启恢复与产物一致、
  DB 全量保留、保留尾部内无操作）

---

# v6.0.0-beta.7 更新日志

> 本版本将沙箱的 Python 环境从「透传宿主系统 `python3`」改为**宿主侧
> uv 预配工作区虚拟环境**：沙箱内不再存在系统 python3，模型按需经新
> 工具 `setup_python_env` 创建 `.venv/` 并安装第三方包（装包在宿主侧
> 执行，绕开沙箱无网络的限制）；同时适配 mirage / pydantic-ai 上游
> API 更名与 per-session workspace 分发。

## 破坏性变更

- **沙箱内系统 python3 移除**（`chat/agent/sandbox.py`）：
  - sandlock 受限子进程的 PATH 从系统目录
    （`/usr/local/bin:/usr/bin:/bin`）改为仅工作区 `.venv/bin`，
    运行时捕获命令从 `python3` 扩为 `python3` + `python`——venv
    创建前两者均不可用（显式 `/usr/bin/python3` 同样被拒，只读授权
    不再覆盖系统目录）
  - 模型须先调用 `setup_python_env` 创建虚拟环境，之后 `python3`
    才可用；venv 随工作区跨会话保留
- **`KanadeWorkspace` 基类随上游更名**（mirage main 前移
  f33a820 → 7c2da21）：官方 `PydanticAIWorkspace` 改名
  `MirageWorkspaceBackend`，方法族从 `aread/awrite/aedit/aexecute`
  重命名为 `read_bytes/write_bytes/stat/list_dir/make_dir/exists/
  remove/realpath/run`（shell 结果字段 `output` → `stdout`）；
  自写的 execute 超时补丁删除（官方 `run` 已内建超时，
  `WorkspaceTimeoutError`）；`ConsoleCapability` 不再传
  `include_background`（上游 API 变化）
- **`ChatDeps` 移除 `backend` 字段**：官方 `MirageWorkspace`
  capability 持有单个固定 workspace，不满足本项目每会话一沙箱；
  新增 `SandboxWorkspaceCapability`，在 `get_workspace` 时从
  `ctx.deps.sandbox` 解析当前会话的根绑定 backend

## 新增

- **`setup_python_env` 工具**（`chat/agent/tool.py`）：在沙箱工作区
  创建/更新 Python 虚拟环境并安装包
  - 宿主侧优先用 uv（`uv venv` + `uv pip install --python`），无
    uv 自动回退宿主 `python3 -m venv` + venv 内 pip；Debian 系缺
    `python3-venv`（ensurepip）时降级 `--without-pip` 裸环境，
    标准库可用，装包时再 `ensurepip` 补 pip，仍失败则提示安装 uv
    或 python3-venv
  - 装包在**宿主侧**执行：沙箱经 sandlock（seccomp）无网络，模型
    在沙箱内 `pip install` 本就不可行，需要任何第三方包都必须走
    本工具
  - 包声明白名单校验：仅接受 `name` / `name[extras]` /
    `name==version` 纯文本形态，URL / git / 本地路径一律拒绝——
    声明会拼进宿主侧命令行，从严防参数注入
  - **entry point 命令动态注册**：装包后扫描 `.venv/bin` 新增的
    可执行名，为其挂独立 SandlockRuntime 路由（captures 命令名、
    同样的内存限额与只读边界），安装自带 CLI 的包后可直接调用
    （如 `pytest --version`），也可 `python3 -m 模块名`
- **`chat.sandbox` 新增配置**：`uv_bin`（默认 `uv`，PATH 名或绝对
  路径）、`uv_python_dir`（默认启动时 `uv python dir` 动态求值）、
  `venv_python`（默认 `3.13`；uv 方式为托管解释器任意版本，原生
  回退按 `python{版本}` 在宿主查找，找不到用宿主默认 python3）、
  `venv_timeout`（默认 300s，宿主侧命令超时）
- **提示词段 `sandbox_python.md`**（`when=python_env_available`）：
  引导「先建 venv 再用 python3、装包必须走工具、装完的 CLI 命令
  直接可调」；uv 与宿主 python3 全缺失时不注入该段（不给无意义
  的引导）

## 改进

- **uv 托管解释器目录授权沙箱只读**：venv 内解释器是指向 uv 托管
  解释器的 symlink，其目录需只读授权才能在沙箱内启动；原生回退
  方式下若解释器位于系统目录之外，安装根同样补进只读授权。原生
  方式必须保持 symlink 形态——`--copies` 的真文件解释器在沙箱内
  会踩 sandlock 的 readlink bug 启动崩溃
- **uv 不可用不再阻断启动**：`_check_uv` 失败降级为 warning 并
  回退宿主 python3（`python{venv_python}` 优先于系统 python3）；
  两者全无时仅提示无法创建 Python 环境，沙箱本身照常可用
- venv 内解释器仍受 `memory_limit` 约束（256M 配置下 1G 分配
  失败，测试覆盖），动态注册的 venv-bin 命令同享该限额
- 依赖升级：`pydantic-ai-backend` 0.2.30 → 0.2.33，mirage main
  commit 前移（`uv.lock`）

## 测试

- `tests/chat/test_mirage_backend.py` 适配新 backend API，新增三组：
  `VenvSetupTest`（venv 前裸 python3 与 `/usr/bin/python3` 均不可用、
  创建后 `sys.executable` 指向 `.venv`、装包后 import / entry point
  CLI / `python3 -m` 三态可用、URL 形态包声明被拒且 venv 未创建、
  内存限额仍生效）、`NativeVenvFallbackTest`（uv 缺失回退
  `python -m venv` 创建与装包、未知版本号回退默认解释器并注明）、
  `UvOptionalStartupTest`（uv 缺失不阻断沙箱启动，回退解释器
  解析正常）
- 新增 `tests/chat/test_prompt_sections.py`：Python 环境提示词段随
  `python_env_available` 变量隐藏/显示
