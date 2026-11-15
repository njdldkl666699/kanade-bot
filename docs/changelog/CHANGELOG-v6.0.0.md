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

---

# v6.0.0-rc.1 更新日志

> 本版本为发布候选版：修复沙箱投入实际使用后暴露的四处问题（中文字体
> 缺失、`read_file` 编码、用户图片不可达、工具异常外抛），并将「删除
> 沙箱工作区」从 `/重置会话` 中拆出为独立命令——重置只清对话，工作区
> （含 `.venv/`）跨重置保留。

## 破坏性变更

- **`/重置会话` 不再删除沙箱工作区**：仅清空会话历史、消息缓冲区与
  记忆上下文并关闭沙箱进程；工作区文件跨重置保留，重置回复中注明
  可用 `/清理工作区` 删除
- **新增 SUPERUSER 命令 `/清理工作区`**（别名 `/清除工作区` /
  `workspace_clear` / `workspaceclear`）：关闭会话沙箱并删除工作区
  目录，**不可逆**且不影响会话历史；沙箱未启用或目录不存在（尚未
  创建、已清理）时分别给出提示，清理出错以错误文本回报

## 修复

- **沙箱缺中文字体**：授权宿主字体目录只读（`/usr/share/fonts`、
  `/usr/local/share/fonts`、`~/.local/share/fonts` 等），并给受限
  进程注入 `HOME`/`XDG_*` 指向工作区 `.home/`——fontconfig 与
  matplotlib 缓存有落点，绘图不再缺 CJK 字体、模型不再联网下载字体；
  沙箱提示词同步注明系统字体目录只读可用
- **`read_file` 非 UTF-8 文本读不出**：编码检测依赖 chardet
  （`pydantic-ai-backends` 可选依赖）此前未安装；补入依赖后实测
  GBK / UTF-16 / UTF-8 均可正确解码
- **用户发送/引用的图片在沙箱内不可达**：图片从宿主
  `cache/auto_clear/` 暂存进沙箱工作区 `images/`（同名不同图自动
  换名，不覆盖旧文件），提示词附带相对路径，`view_image` /
  `image_search` / `send_image` 等工具均可取用
- **工具异常外抛炸穿 agent 运行**：`send_file` 上传、OneBot 发送
  消息、`view_image` 下载补异常兜底，失败以错误文本返回模型，
  不再终止整轮回复

## 改进

- 日志文件（`cache/kanade.log`）级别 DEBUG → WARNING，磁盘不再被
  全量调试日志快速填充
- 帮助文档同步：`/清理工作区` 命令说明与命令总览（`config/help/`）；
  `chat_configs.json` 默认路径说明修正为 `config/chat/chat_configs.json`
- 版本号 v6.0.0-beta.7 → v6.0.0-rc.1

## 测试

- 新增 `tests/chat/test_sandbox_fonts.py`：字体目录授权与
  `HOME`/`XDG_*` 注入的运行时配置单元测试、图片暂存换名逻辑测试，
  及真实 sandlock 受限进程内字体可读、缓存可写、宿主家目录仍被拒
  的端到端验证

---

# v6.0.0-rc.2 更新日志

> 本版本新增 **Agent 定时任务**：模型可调用 `schedule_task` 工具设定
> 一次性定时任务，到点后系统主动唤醒会话并把回复发回原群/私聊。
> 同时把消息投递逻辑抽为独立模块（被动回复与主动发送共用），并让
> `render_html_image` 支持直接渲染工作区内的 HTML 文件（相对资源以
> 工作区根解析）。

## 新增

- **Agent 定时任务**（`chat/agent/schedule.py`）：
  - 新工具 `schedule_task`：以自然语言描述任务，`run_at`（ISO 8601
    时刻）与 `delay_minutes`（延迟分钟数）二选一指定触发时间，返回
    任务 ID；非法时间参数以文本反馈模型自行纠正
  - 到点后由 APScheduler 回调，以**显式 system_notification + 空用户
    消息**运行一轮唤醒 agent，回复主动发送回原会话（群或私聊）；
    到点通知包含任务描述、创建者与设定时间
  - 任务持久化为 JSON（临时文件 + 原子替换），含会话快照与 bot_id，
    重启后恢复调度；已过期任务跳过并清理；触发失败按配置有限重试，
    耗尽后丢弃
  - 触发前预检：目标会话/用户已拉黑则静默放弃；创建者水晶余额为负
    则不唤醒 agent，直接发文本告知；正常执行按实际 usage 扣创建者
    水晶（峰谷按触发时刻判定）
- **新命令**：`/任务列表`（`chat_tasks`）列出当前会话待触发任务；
  `/取消任务 <ID>`（`chat_task_cancel`）按 ID 取消，仅限本会话创建
  的任务
- **新配置段 `chat.scheduled_task`**：`data_file`（持久化文件名）、
  `retry_limit`（重试次数，不含首次）、`retry_delay_minutes`（重试
  间隔），默认 `scheduled_tasks.json` / 2 / 5 分钟
- **消息投递模块**（`chat/deliver.py`）：从 chat.py 抽出
  `extract_segments_preserving_code`（代码块保护拆分 + 表情包引用
  替换）与分级批量发送（≤5 按条、≤10 合并转发、>10 合并相邻文本 +
  长内容转图），新增 `send_onebot_proactive` /
  `send_text_onebot_proactive` 主动发送；被动回复与定时任务共用
  同一套解析与发送逻辑
- **`render_html_image` 支持渲染 HTML 文件**：新增 `file_path` 参数，
  直接渲染沙箱工作区内的 HTML 文件（与 `html` 二选一），并以
  `template_path` 把 HTML 内的相对资源路径（图片、iframe 等）锚定到
  工作区根解析；为此 htmlrender 改用回植 PR #112 的 fork
  （`backport/pr-112`，为 `html_to_pic` 引入 `template_path` 参数）

## 改进

- 路径协议提示词（`tool_usage.md`）改写：本地路径一律为相对沙箱
  工作区根的裸路径，`file://` 等协议前缀不再支持——与沙箱实际
  行为对齐
- `send_and_wait` 新增 `system_notification` 显式注入参数：不消费
  排队通知槽位（留待下一轮），且单独视为可运行内容（空 prompt 也
  能触发生成）
- 提示词新增 `<schedule_task>` 工具使用指南（适用场景、时间换算、
  会话级语义）
- 版本号 v6.0.0-rc.1 → v6.0.0-rc.2

## 测试

- 新增 `tests/chat/test_schedule.py`：时间解析（ISO 8601 / 延迟
  分钟、时区归一、过去时间与双参数冲突拒绝）、JSON 持久化原子写入、
  任务生命周期（创建、取消、重启恢复跳过过期、失败重试）单元测试；
  桩掉 nonebot / apscheduler / crystal 重依赖，只测纯逻辑

---

# v6.0.0 更新日志

> 正式版收尾：移除已无引用的 `openai-proxy` 子项目，mirage 升级后
> 沙箱运行时配置改为类型化的 `SandlockConfig`，定时任务在群聊中
> 主动回复时 @创建者。自 beta.1 起的全部变更（Pydantic AI 迁移、
> mirage + sandlock 沙箱、按 Token 计费、会话常驻沙箱、Agent
> 定时任务等）见上方各版本日志。

## 移除

- **`openai-proxy` 子项目**（Go，约 1100 行）：自 beta.1 改为
  直连 OpenAI 兼容端点后不再被任何模块引用，整个目录删除；
  上游请求抓取（`cache/misc_not_plugin/`）与图片剥离等历史
  用途已由直接配置覆盖

## 改进

- **沙箱运行时配置类型化**：mirage git 依赖升级（rev `6c0b322`）
  后，`sandbox_runtime_config` 返回值从裸 dict 改为
  `SandlockConfig`（字段 `fs_readable` / `fs_writable` /
  `max_memory` / `env` 不变），配置错误提前到构造期暴露；
  `tests/chat/test_sandbox_fonts.py` 同步改为属性访问
- **定时任务群聊回复 @创建者**：`send_onebot_proactive` 在群聊
  场景下首条消息前拼接 at 创建者段（私聊不变），主动唤醒的
  回复不再无提示混入群消息流
- `schedule.py` 的 `is_banned` / `send_onebot_proactive` 等
  延迟导入上移至模块顶部——循环依赖已随模块拆分消除，不再
  需要 workaround
- README 徽章与简介从 OpenAI Agents SDK 更正为 Pydantic AI
- 依赖升级：orjson 3.12.0 → 3.13.0、tenacity 9.1.4 → 9.2.1
- 版本号 v6.0.0-rc.2 → v6.0.0

---

# v6.0.0 正式发布日志

> 本节面向使用 Bot 的普通用户，仅列出可直接感知的变化，技术细节见上方各版本的开发日志。6.0 为大版本更新，聊天与总结模块的底层实现已整体重构，主要收益是回复稳定性提升、故障率下降，并新增若干可直接使用的功能。

## 聊天体验

- **长回复自动续写**：模型输出因长度上限被截断时，Bot 会自动续写至内容完整，不再出现半途中断。
- **中断即时生效**：`/中断会话` 现在会真正取消在途的模型请求与工具调用，不再出现已下达中断指令却仍在继续输出的情况。
- **局部故障不影响整轮回复**：发送文件、发送图片、以图搜图等环节失败时，错误将作为信息回传给模型继续处理，而非直接终止本轮回复。
- **推理内容不再混入正文**：具备推理能力的模型（如 DeepSeek）的思考过程不会再混杂于正式回复内容中。
- **用户图片可用**：发送或引用的图片将自动存入 Bot 工作区，识图、以图搜图、二次处理等相关功能均可正常调用。

## 新增功能

### 定时任务

以自然语言向宵崎奏描述一件未来需要执行的事务（如「半小时后提醒我喝水」「明天早上八点叫我起床」），Bot 会创建定时任务，到点后在当前会话主动提醒或汇报。

- 任务为一次性触发，仅支持 QQ（OneBot v11）群聊与私聊会话。
- 群聊中到点触发时会 @ 任务创建者，不会无提示地插入群消息流。
- `/任务列表` 可查看待触发任务，`/取消任务 <任务ID>` 可取消（仅限本会话创建的任务）。
- 任务执行时按实际用量扣除**创建者**的水晶；余额为负时任务将被跳过并告知。Bot 重启期间错过的任务不会补发。

### 沙箱

Bot 现为每个会话提供独立工作区，可在其中读写文件、执行命令、处理数据与生成图表。

- 工作区文件跨对话保留，重新开始对话不会丢失；如需清空，可使用 `/清理工作区` 一并删除（不可逆）。
- 涉及第三方库时，Bot 会先创建 Python 环境再安装并运行，也可直接调用安装后附带的命令行工具。
- 绘图已内置中文字体，不会再出现中文显示为方块的情况。
- 工作区运行在独立隔离环境中，环境之外的路径无法访问。

## 命令变更

| 命令 | 说明 |
| --- | --- |
| `@宵崎奏 <消息>` | 与 Bot 对话（用法不变） |
| `/会话统计` | 查看当前会话的 token 消耗、消息条数、工作区文件等 |
| `/压缩会话` \* | 手动将早期历史压缩为一条摘要，并返回压缩前后的消息数与 token 对比 |
| `/清理工作区` \* | 删除当前会话的工作区文件（不可逆），不影响会话历史 |
| `/重置会话` \* | **仅重置对话，不再删除工作区**（此前会连同工作区一并删除） |
| `/中断会话` \* | 中断当前正在进行的回复，等待中的消息照常处理 |
| `/任务列表` | 列出本会话待触发的定时任务 |
| `/取消任务 <任务ID>` | 取消本会话创建的定时任务 |

\* 需要超级管理员权限。此外，`重置会话 / 会话重置`、`中断会话 / 会话中断`、`压缩会话 / 会话压缩` 两种语序均可触发。

## 计费变更

- **聊天与总结改为按 Token 计费**：不再按固定数值扣除，改为依据实际用量结算——对话越短费用越低，单轮消耗设有最低额度。
- **缓存命中部分不计费**：输入中命中缓存的部分不计费，仅对未命中部分计费；输出（含推理内容）按全量计费。
- **分时段费率**：工作日 9:00–12:00 与 14:00–18:00 适用高峰费率；午休时段、夜间、周末及法定节假日适用较低费率。
- **使用门槛下调**：聊天与总结的准入条件由「余额达到指定数值」调整为「余额大于 0」；余额为负时定时任务将被跳过。

## 修复

- **非 UTF-8 编码文本无法读取**：GBK、UTF-16 等编码的文件（含用户发送的文本）现均可正确读取。
- **用户图片在沙箱内不可达**：图片现会存入工作区，相关工具均可正常调用。
- **绘图缺失中文字体**：已补齐字体支持，matplotlib 等绘图不再出现方块字。
- **输出长度上限设置未生效**：已修复部分接口不识别「最大输出长度」配置的问题。
- **重置会话误删工作区**：已拆分为独立命令，重置会话不再删除文件。

## 其它

- **GitHub 链接自动卡片**：群聊中发送 GitHub 链接时，Bot 将以卡片形式回复仓库信息。
- **十连抽卡渲染提速**：卡面缩略图引入磁盘缓存，十连渲染速度有明显提升。
- **启动横幅可配置**：可在配置中关闭启动横幅。
- **日志占用减少**：日志文件默认不再记录全量调试信息。

## 升级须知

- **配置与 5.x 不兼容**：请参照 `config-example.yaml` 重新配置，重点关注新增的 `chat.billing`（计费）、`chat.compaction`（压缩）、`chat.memory`（记忆）、`chat.scheduled_task`（定时任务）、`chat.sandbox`（工作区）等配置段。
- **会话历史不迁移**：升级后会话从零开始，Bot 不会保留 5.x 期间的对话内容（总结的消息记录缓存仍会保留）。
- **工作区功能依赖 `sandlock`**：未安装时 Bot 将启动报错；如暂不使用，可通过配置关闭沙箱。

