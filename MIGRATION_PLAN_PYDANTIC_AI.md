# chat / summary 迁移到 Pydantic AI 计划

> 起草：2026-10-03；**第三轮修订：方案已全部定案**（沙箱选型、压缩策略、记忆范围）
> 目标：**openai-agents SDK → Pydantic AI** 全量迁移
> 状态：**方案已定，可执行**

## 摘要（TL;DR）

**可行性：高。** 已在 `.venv`（`pydantic-ai-slim==2.54.0` + `pydantic-ai-harness==0.54.0`）实测验证本项目最关心的能力全部原生具备。**核心动机成立**——`OpenAIChatModel` 是一等公民，与本项目主用的 Chat Completions 完全对口。

**四个意外收获**：

| 收益                         | 说明                                                                                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **删掉一整套自建 hack**      | `finish_reason` 是 `ModelResponse.finish_reason` **原生字段**。现有 `LengthTrackedChatCompletionsModel` + `ContextVar` + 续写状态机（~100 行绕行）整体删除 |
| **压缩策略开箱可用且更严谨** | 官方策略**自动保持 tool_call/tool_return 配对**（实测 32 条→6 条零孤儿）。现有 `LocalCompactionSession`（alpha 库）可整包替换                          |
| **工具条件注册有官方等价物** | `PrepareTools` capability 精确复刻 `is_enabled` 语义（实测关闭时工具完全不入 schema）                                                                      |
| **内置防无限循环**           | 默认 `UsageLimits(request_limit=50)`，本项目当前无此约束                                                                                                   |

**主要代价**：会话历史要自研（`SQLiteSession`/`StepPersistence` 均非其职责）、沙箱要从 `SandboxAgent` 改为 `Workspace` capability、MCP 换 fastmcp 客户端。

### 已定案（2026-10-03，用户决策）

| # | 事项 | 决策 |
| --- | --- | --- |
| 1 | 是否迁移 | **值得**，全量迁移，**不再使用 openai-agents**（summary / image_caption 一并迁） |
| 2 | 沙箱 | **mirage + sandlock**，包装为 `WorkspaceBackend`；**不用 bubblewrap**（实测可读到 bot 的 `config.yaml`，含密钥） |
| 3 | 会话历史 | **自研 SQLite Session**。DB **append-only 保留全量**；**压缩事件落库**，恢复时重放使内容与关闭前一致（保护 KV Cache） |
| 4 | 会话压缩 | **S3 `TieredCompaction`**（`ClearToolResults` → `SummarizingCompaction`）。**不用滑动窗口**——本项目 KV Cache 命中率关键，滑动窗口每次丢前缀。**必开 `min_clear_tokens`**：清理收益太小则不清理，避免白白弄坏缓存。生产中观察后再调 |
| 5 | 记忆 | **本次不动**，保持现有 `MemoryStore`。Q2 选 A（不做有界自动注入） |
| 6 | 阶段顺序 | 0 → 1 → 2 → 3 → 4 |

> **记忆系统后续会整体重写**，故本次迁移不触碰——避免同一改动里既换框架又换记忆，出了问题难以定位。

---

## 0. 三层概念的区分（先厘清，避免混淆）

Pydantic AI 官方把「与消息相关」的能力拆成三类，**职责不同，不可互相替代**：

| 概念                            | 是什么                                 | 存什么                   | 生命周期             | 本项目对应                        | 本次处理             |
| ------------------------------- | -------------------------------------- | ------------------------ | -------------------- | --------------------------------- | -------------------- |
| **会话历史**（message history） | 消息列表本体                           | `list[ModelMessage]`     | 跨轮次、跨进程       | `SQLiteSession`                   | **自研替换**（4.5.1） |
| **会话压缩**（compaction）      | 每次请求前**改写历史**以适配上下文窗口 | 裁剪/摘要后的消息        | 随请求演进           | `LocalCompactionSession`（alpha） | **换官方 S3**（4.5.2） |
| **记忆**（memory）              | 模型主动读写的长期笔记                 | Markdown 文件 / 独立存储 | **刻意超出单次会话** | 自研 `MemoryStore`                | **不动**（4.5.3）    |

官方原文佐证（`message-history` 页）：

> | Capability | What it stores | Scoped by |
> | `StepPersistence` | Message snapshots taken at settled points **in a run**… | `conversation_id` and `run_id` |
> | `Memory` | Markdown notes the agent writes and reads itself, **deliberately outliving any single conversation** | A namespace you choose |

---

## 1. 为什么迁：动机是否成立

### 1.1 原始动机验证：Chat Completions 优先

**结论：动机成立且理由比预想的更强。**

实测 `pydantic_ai.models.openai` 同时提供 `OpenAIChatModel`（Chat Completions）与 `OpenAIResponsesModel`（Responses），二者对等：

```python
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
client = AsyncOpenAI(base_url="https://api.deepseek.com/v1", api_key=...)
model = OpenAIChatModel("deepseek-chat", provider=OpenAIProvider(openai_client=client))
```

对比现状：`openai-agents` 的 sandbox、apply_patch 等新能力**优先甚至仅面向 Responses API**，本项目因此被迫在 `manager.py` 注释里明确放弃 `Filesystem` capability：

> 沙箱能力只留 Shell：Filesystem 的 apply_patch 是 FREEFORM/grammar 工具，仅 Responses API 支持，Chat Completions 下会在工具转换时抛 UserError

这是**真实且长期存在的功能缺口**（模型没有文件编辑工具，只能用 shell 拼）。

### 1.2 但要注意：mirage 已经补上了这个缺口

上一轮刚完成的 mirage 迁移，让 `exec_command` + 完整 shell（`cat`/`sed`/`grep`/`python3`）可用，**文件编辑能力实际已经不缺**。所以 1.1 的论据强度下降——真正剩下的是「未来用不上 apply_patch 这类原生工具」，而非「现在做不到」。

**这一点必须诚实记账**：如果 mirage 已满足需求，迁移的收益就不再是"补功能"，而是"换一套更顺手的抽象 + 删掉 hack"。

### 1.3 迁移的真实收益（重新排序）

| #   | 收益                               | 权重   | 说明                                                         |
| --- | ---------------------------------- | ------ | ------------------------------------------------------------ |
| 1   | 删除 `finish_reason` hack          | **高** | 约 100 行自建绕行变原生字段；这类 hack 每次 SDK 升级都要维护 |
| 2   | Chat Completions 与 Responses 平权 | **高** | 未来若要迁 Responses API，成本更低                           |
| 3   | `Workspace` 抽象更干净             | **中** | mirage 可包装复用；但需自己实现 17 个方法                    |
| 4   | 内置 usage/重试/超时治理           | **中** | `UsageLimits` / `Retries` / `Timeouts` 开箱可用              |
| 5   | 官方 Agent Skills 文档             | **中** | 包内自带 `.agents/skills/`，对 AI 协作友好                   |
| 6   | 去掉 `pop_item` 回退 hack          | **低** | 空响应回退逻辑在 Pydantic AI 有更直接的写法                  |

### 1.4 反对意见（必须正视）

- **Pydantic AI 更年轻**：2.54.0 vs openai-agents 0.23.1，本项目已踩过三方库早期期的坑（`openai-agents-context-compaction` 至今是 alpha）。
- **Harness / Workspace 较新**：`Workspace` 是新抽象，文档明确提到它源自内部 `harness` 包，接口可能演进。
- **生态迁移成本**：MCP 从 `mcp` SDK 切到 `fastmcp` 客户端；sandbox 从 `SandboxAgent` 换成完全不同的模型。
- **第三次迁移的疲劳**：项目刚完成 Copilot SDK → openai-agents。现在再迁，需要非常明确的收益才划算。

---

## 2. 实测验证：能力矩阵

以下全部在 `.venv`（`pydantic-ai-slim==2.54.0`，Python 3.14）用假 OpenAI Chat Completions 服务端到端跑通，非文档推断。

### 2.1 已验证通过 ✅

| 能力                         | 验证方式                                   | 结果                                                              |
| ---------------------------- | ------------------------------------------ | ----------------------------------------------------------------- |
| Chat Completions 端到端      | 假 server + `OpenAIChatModel`              | ✅ 请求走 `/chat/completions`，SSE 正常                            |
| 流式文本                     | `run_stream()` + `stream_text(delta=True)` | ✅ 逐 token 产出                                                   |
| **`finish_reason` 截断检测** | 服务端返 `finish_reason="length"`          | ✅ `all_messages()[-1].finish_reason == 'length'` —— **原生字段**  |
| 工具调用循环                 | 服务端先返 tool_calls 再返文本             | ✅ `ToolCallPart` → `ToolReturnPart` → 最终输出                    |
| 用量统计                     | `RunUsage`                                 | ✅ `input_tokens`/`output_tokens`/`cost()`（注意是**属性**非方法） |
| 依赖注入（deps）             | `deps_type` + `RunContext[Deps]`           | ✅ 替代现有 `ChatContext` + `RunContextWrapper`                    |
| 多模态                       | `BinaryContent` / `ImageUrl`               | ✅ 可用                                                            |
| 内置限额                     | 默认 `request_limit=50`                    | ✅ 超限抛 `UsageLimitExceeded`（**需显式配置**，见 4.4）           |

### 2.2 需额外依赖 / 有坑 ⚠️

| 项                  | 状态                                                   | 处置                                                                                 |
| ------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------ |
| **MCP**             | ⚠️ `ImportError: Please install the fastmcp client`     | 加 `pydantic-ai-slim[mcp]`；类名也不同：`MCPToolset`（非 `MCPServerStreamableHttp`） |
| **Harness（沙箱）** | ⚠️ 不在 slim 里，是独立包 `pydantic-ai-harness==0.54.0` | 已定：mirage 自包（见 4.3）                                                                        |
| **usage API 形态**  | ⚠️ `result.usage` 是**属性**，不是方法                  | 与 openai-agents 不同，注意迁移期踩坑                                                |
| **banner 输出**     | ⚠️ 首次 import 打印 ASCII banner                        | 设 `PYDANTIC_AI_NO_BANNER=1`                                                         |
| 默认请求上限        | ⚠️ `request_limit=50` 对长工具链任务可能偏紧            | 用 `UsageLimits(request_limit=None)` 或按场景配置                                    |

### 2.3 沙箱抽象对照

`Workspace` 协议（17 个方法，`pydantic_ai.workspaces.Workspace`）：

```
attached, backend, durable_policy, exists, list_dir, make_dir, read_bytes,
read_only, read_text, realpath, ref, remove, resolve, run, stat,
working_dir, write_bytes, write_text
```

这是**窄接口**，且官方提供 `pydantic_ai.workspaces.conformance.WorkspaceBackendSuite` 用于验证自定义后端 —— 对包装 mirage 是好消息。

---

## 3. 现有实现盘点与映射

### 3.1 迁移影响面（实测统计）

```
kanade_bot/plugins/chat/agent/manager.py     596 行  ← 核心，最大改造量
kanade_bot/plugins/chat/agent/tool.py        445 行  ← 工具签名全改
kanade_bot/plugins/chat/agent/memory.py      318 行  ← 不受影响
kanade_bot/plugins/chat/agent/sandbox.py     267 行  ← 重新包装
kanade_bot/utils/agents_runtime.py           238 行  ← 大幅简化
kanade_bot/plugins/chat/agent/image_caption.py 75 行 ← 小改
kanade_bot/plugins/chat/agent/context.py      37 行  ← 被 deps 取代
kanade_bot/plugins/summary/summarizer.py       —     ← 只用 Agent，小改
                              合计约 1976 行
```

`openai-agents` 引用共 20+ 处，集中在 `manager.py` 与 `tool.py`。

### 3.2 API 映射表

| 现（openai-agents 0.23.1）                 | 迁（Pydantic AI 2.54.0）                      | 难度   | 备注                                             |
| ------------------------------------------ | --------------------------------------------- | ------ | ------------------------------------------------ |
| `Agent(model=..., instructions=...)`       | `Agent(model, instructions=...)`              | 低     | `instructions` 参数名一致                        |
| `instructions` 动态函数                    | `instructions` 函数（签名不同）               | 低     | 现用 `RunContextWrapper` → 改 `RunContext[Deps]` |
| `RunContextWrapper[ChatContext]`           | `RunContext[ChatDeps]`                        | 低     | `ctx.context` → `ctx.deps`                       |
| `@function_tool`                           | `@agent.tool` / `@agent.tool_plain`           | 低     | 装饰器从闭包改为类方法式注册                     |
| `is_enabled=` 条件启用                     | `prepare_tools` 钩子 / toolset 过滤           | **中** | 现有 3 个工具用 `is_enabled`（memory/tts）       |
| `ToolOutputImage` / `ToolOutputText`       | 返回 `BinaryContent` / `str`                  | 低     | 更直接                                           |
| `Runner.run_streamed(...)`                 | `agent.run_stream(...)`                       | 低     |                                                  |
| `stream_events()`                          | `run_stream()` 上下文管理器                   | **中** | 事件模型完全不同，见 4.2                         |
| `item.name == "tool_called"`               | 直接用 `stream()` 事件或工具内埋点            | **中** |                                                  |
| `MessageOutputItem` + `ItemHelpers`        | `ModelResponse.parts` 里 `TextPart`           | 低     |                                                  |
| `SandboxAgent(capabilities=[Shell()])`     | `Agent(..., capabilities=[...])` + Workspace  | **高** | 见 4.3                                           |
| `SandboxRunConfig(session=...)`            | `agent.run(..., workspace=...)`               | **高** |                                                  |
| `SQLiteSession` + `LocalCompactionSession` | **无对应物，需自研**                          | **高** | 见 4.5                                           |
| `session.pop_item()` 回退                  | 直接裁剪 `message_history` 列表               | 低     | 反而更简单                                       |
| `MCPServerStreamableHttp` + `ToolFilter`   | `MCPToolset`                                  | **中** | 需 `fastmcp`                                     |
| `agents.memory.SQLiteSession`              | 自研 SQLite 存储                              | **高** |                                                  |
| `OpenAIChatCompletionsModel`               | `OpenAIChatModel`                             | 低     |                                                  |
| `ModelSettings(reasoning=, max_tokens=)`   | `ModelSettings(max_tokens=, temperature=...)` | 低     | reasoning 映射需核对                             |
| `set_tracing_disabled(True)`               | `Instrumentation`（默认关闭）                 | 低     |                                                  |
| `TResponseInputItem`                       | `ModelMessage` 列表                           | 低     |                                                  |
| `begin_length_tracking()` + ContextVar     | **删除**，读 `finish_reason`                  | 低     | 净减代码                                         |

### 3.3 可以直接删掉的东西

| 删除项                                       | 位置                | 行数 | 原因                                  |
| -------------------------------------------- | ------------------- | ---- | ------------------------------------- |
| `LengthTrackedChatCompletionsModel`          | `agents_runtime.py` | ~100 | `finish_reason` 原生可用              |
| `_finish_reason_holder` ContextVar           | `agents_runtime.py` | ~10  | 同上                                  |
| `begin_length_tracking` 及其 ContextVar 注释 | `agents_runtime.py` | ~15  | 同上                                  |
| `ChatContext` dataclass                      | `context.py`        | 37   | 被 `RunContext[Deps]` 取代            |
| `EMPTY_RESPONSE_MAX_RETRIES` 回退机制        | `manager.py`        | ~30  | Pydantic AI 的 output 校验/重试更自然 |
| `CONTINUE_PROMPT` 续写状态机                 | `manager.py`        | ~20  | `finish_reason` 驱动，更简单          |
| `openai-agents-context-compaction` 依赖      | `pyproject.toml`    | —    | 需自研替代，见 4.5                    |

**净减约 200 行绕行代码**，这是本次迁移的主要工程价值。

---

## 4. 分项设计决策

### 4.1 模型层（简单，直接替换）

```python
# kanade_bot/utils/pai_runtime.py（替代 agents_runtime.py）
_client_cache: dict[tuple, AsyncOpenAI] = {}
_model_cache: dict[tuple, OpenAIChatModel] = {}

def get_model(config: BaseAgentConfig) -> OpenAIChatModel:
    client = AsyncOpenAI(base_url=..., api_key=..., default_headers=..., max_retries=5)
    return OpenAIChatModel(config.model, provider=OpenAIProvider(openai_client=client))

def build_model_settings(config) -> ModelSettings:
    return ModelSettings(max_tokens=config.max_output_tokens)
```

**要点**：
- `reasoning_effort` 映射需核对 —— Pydantic AI 的 `ModelSettings` 没有 `reasoning` 字段直连，DeepSeek 的 thinking 可能要走 provider-specific 设置或 profile。**这是唯一需要实测的模型层细节。**
- 可考虑用 `profile=` 预设取代手写 settings（`ModelProfileSpec`）。

### 4.2 流式与事件（本项目最复杂的部分）

现状是 `Runner.run_streamed()` + `stream_events()`，靠事件名 `run_item_stream_event` / `name == "tool_called"` 分流，并有：
- watchdog 超时（相邻事件间隔）→ cancel
- 空响应检测 → 回退重发
- 截断续写

Pydantic AI 侧对应：

```python
async with agent.run_stream(prompt, deps=deps, message_history=hist,
                            usage_limits=UsageLimits(request_limit=None)) as r:
    async for event in r:
        # 事件类型：PartStartEvent / PartDeltaEvent / FunctionToolCallEvent / ...
        pass
```

**需要重新设计的点**：

| 现有机制               | Pydantic AI 中的做法                                                   | 风险                             |
| ---------------------- | ---------------------------------------------------------------------- | -------------------------------- |
| 相邻事件 watchdog 超时 | `asyncio.wait_for(aiter.__anext__(), timeout)`，机制可沿用             | 低                               |
| `stream_events()` 分流 | `run_stream` 的事件类型（`PartStartEvent`/`FunctionToolCallEvent` 等） | **中**：事件模型不同，需重写分流 |
| `tool_called` 判定     | `FunctionToolCallEvent`                                                | 低                               |
| 空响应检测             | 检查 `r.all_messages()[-1]` 有无 `TextPart` 且无工具调用               | 低                               |
| 截断续写               | `finish_reason == 'length'` → 续写                                     | **更低**（原生字段）             |
| 历史回退               | `message_history: list[ModelMessage]` 直接切片                         | 低                               |

**建议**：阶段一先实现最小可用流式（文本 + 工具调用识别），watchdog/空响应/续写逐个迁移，每迁一个跑一次回归。

### 4.3 沙箱：**选定 mirage + sandlock**（已决策）

> **决策（2026-10-03）：采用 mirage + sandlock，包装为 `WorkspaceBackend`。不用 bubblewrap。**
> 以下保留 bubblewrap 的实测数据作为决策依据与回归对照。

#### 4.3.0 实现要点

```python
# kanade_bot/plugins/chat/agent/sandbox.py（改造）
class MirageBackend(WorkspaceBackend):
    """把现有 mirage Workspace 适配到 Pydantic AI 的 Workspace 协议。

    隔离语义完全沿用现状（实测已验证）：
    - 工作区外路径不存在（VFS 隔离），模型读不到 bot 配置/密钥
    - python3 经 sandlock（Landlock + seccomp）约束，max_memory 生效
    - 挂载前缀 = 宿主 realpath，免 FUSE
    """

    async def run(self, command, *, cwd=None, env=None, timeout=None) -> CommandResult: ...
    async def read_bytes(self, path: Path) -> bytes: ...
    async def write_bytes(self, path: Path, content: bytes) -> None: ...
    async def list_dir(self, path: Path) -> list[FileEntry]: ...
    async def stat(self, path: Path): ...
    async def remove(self, path: Path) -> None: ...
    async def exists(self, path: Path) -> bool: ...
    async def make_dir(self, path: Path) -> None: ...
    # ... 按 WorkspaceBackend 协议补齐
```

**迁移范围**：现有 mirage 集成（`sandbox.py` 267 行 + 23 项测试）**逻辑不变**，只在上面加一层协议适配。
`SandboxManager` 的 TTL/LRU/池化是框架无关的应用层代码，原样保留。

#### 4.3.0.1 ⚠️ 生产内核 6.8：Landlock ABI 降级（**必须处理**）

**生产环境内核为 6.8 → Landlock ABI 仅 v4**，而 sandlock 要求 **ABI v6**（Linux 6.12+）。
默认行为是 **Strict**：required protection 不可用时 sandlock **拒绝启动**，
即沙箱在生产上会直接不可用。**必须显式降级。**

各 protection 的 ABI 下限（官方 `sandbox-reference.md`）与 6.8 的可用性：

| Protection | ABI floor | 6.8 (ABI v4) | 降级后是否还生效 |
| --- | --- | --- | --- |
| `FsRefer` | v2 | ✅ | ✅ |
| `FsTruncate` | v3 | ✅ | ✅ |
| `NetTcp` | v4 | ✅ | ✅ |
| `FsIoctlDev` | **v5** | ❌ | ❌ 降级后不强制 |
| `SignalScope` | **v6** | ❌ | ❌ 降级后不强制 |
| `AbstractUnixSocketScope` | **v6** | ❌ | ❌ 降级后不强制 |

**降级手段（CLI 原生支持，实测可用）**：

```bash
sandlock run --allow-degraded signal-scope \
             --allow-degraded abstract-unix-socket-scope \
             --allow-degraded fs-ioctl-dev \
             -r <paths> -- <cmd>
```

- `--allow-degraded <PROT>`：宿主 ABI 支持就强制，不支持就静默跳过；
- 实测三个名称均被 CLI 接受（exit 0）；无效名会报错并列出合法值；
- 注意 `fs-refer` **不能** `--disable`（内核默认就拒绝 REFER，禁它只会更严）。

**实现方式：PATH shim 包装**（因 mirage adapter 硬编码 argv，无透传接口）

实测 mirage `SandlockRuntime.run_process` 构造的完整 argv：

```
run -r /usr -r /etc -r /proc -r /dev -r <workspace> -w <workspace> -m 256M
    --clean-env --env PATH=... --env PWD=...
    -- python3 -c "print(1)"
```

源码里是 `argv = ["run", *policy_argv(), "--clean-env"] + envs + ["--", *cmd]`，
**没有 extra_args 接口**。但 `--allow-degraded` 插在 `--` 之前是安全的
（不会被当成被沙箱命令的参数）。

因此在 `PATH` 前面放一个 wrapper：

```python
# kanade_bot/plugins/chat/agent/sandbox.py（新增）
# 注意：wrapper 解释器必须是 /bin/sh（POSIX），不能用 bash 专有语法。
# 真实路径从 shutil.which("sandlock") 在**wrapper 生成时**求值并写入，
# 不要硬编码路径（部署环境各不相同）。
SANDLOCK_WRAPPER = """#!/bin/sh
# 注入 Landlock 降级参数后 exec 真正的 sandlock。
# 必须在 "--" 之前插入，否则会被当成被沙箱命令的参数。
# 用 `sub=$1; shift` 而非 "$@" 切片：${@:2} 是 bash 扩展，/bin/sh(dash) 不支持。
sub="$1"; shift
exec "{real_sandlock}" "$sub" \\
    --allow-degraded signal-scope \\
    --allow-degraded abstract-unix-socket-scope \\
    --allow-degraded fs-ioctl-dev \\
    "$@"
"""
```

**两个实测踩到的坑**：

| 坑 | 现象 | 正解 |
| --- | --- | --- |
| `${@:2}` 是 bash 扩展 | `/bin/sh: 2: Bad substitution`，wrapper 静默失效（沙箱实际未降级） | `sub="$1"; shift` 后用 `"$@"` |
| 硬编码 `/usr/local/bin/sandlock` | 本机真实路径是 `/home/cola/Softwares/sandlock/sandlock`（由 `~/.profile` 注入 PATH），写死会找不到 | 生成 wrapper 时用 `shutil.which("sandlock")` 求值并写入 |

**防「静默失效」**：wrapper 生成后、应用启动前自检一次——用 wrapper 跑一条
带 v6 protection 的命令，确认 sandlock 没报「protection unavailable」。
否则沙箱看似运行、实际未降级，属于最危险的失败模式。

启动时把 wrapper 写到 `cfg.sandbox.bin_dir`（默认 `sandboxes/.bin/`），
并确保该目录在 `PATH` 中**优先于**真实 sandlock；mirage 用
`shutil.which("sandlock")` 查找，会命中 wrapper。

> **PATH 可见性**：bot 由用户在交互式 shell 手动启动，PATH 正常继承
> （实测 `zsh -ic 'which sandlock'` 与 Python 子进程均可见）。若将来改为
> systemd / supervisor 托管，需改用绝对路径配置 `landlock_real_binary`。

**降级后的安全影响（必须写入运维文档）**：

| 维度 | 降级后状态 |
| --- | --- |
| **文件系统隔离（mirage 隔离的主体）** | ✅ **不受影响** —— 靠 Landlock FS rules（ABI v1+）+ mirage VFS，工作区外路径仍不存在 |
| 内存限额 `max_memory` | ✅ 不受影响（rlimit，非 Landlock） |
| `SignalScope` | ❌ 沙箱内进程可 signal 宿主同用户进程 |
| `AbstractUnixSocketScope` | ❌ 可连接宿主的 abstract unix socket |
| `FsIoctlDev` | ❌ 可对设备节点做 ioctl |

即：**降级削弱的是「防进程逃逸」维度，不破坏「工作区隔离」主线**。
对本项目而言，模型能读到 bot 自身配置的风险来自 mirage VFS（不依赖 ABI v6），
因此该风险在 6.8 上依然被阻断。

**启动自检**：用 `sandlock check` 读 ABI 并决策

```python
def check_sandlock() -> tuple[int, int]:
    """返回 (host_abi, required_abi)；不可用时抛 SandlockUnavailableError"""
    out = subprocess.run(["sandlock", "check"], capture_output=True, text=True)
    # 解析 "Landlock: ABI v8" 与 "Minimum required: ABI v6"
```

- ABI ≥ v6 → 不需要 wrapper，直接用真 sandlock；
- v4 ≤ ABI < v6 → 启用 wrapper（降级），**并在启动日志中明确打印失去的保护**；
- ABI < v4 或 Landlock 不可用 → 抛 `SandlockUnavailableError`（此时连 FS 隔离都
  无法保证，必须报错而非静默降级）。

配置项：

```python
landlock_degrade: Literal["auto", "always", "strict"] = "auto"
"""auto=按 sandlock check 的结果自动决定；always=始终注入降级参数；
strict=ABI 不足 v6 即报错（不降级）"""

landlock_real_binary: str | None = None
"""真实 sandlock 可执行文件绝对路径。

留空（默认）= 生成 wrapper 时用 shutil.which("sandlock") 动态求值。
**推荐留空**，以适配各部署环境不同的安装路径。
仅当 sandlock 不在 PATH（如 systemd 托管）时才需显式指定。"""
```

> 注意：`landlock_real_binary` 是**绝对路径**，不是命令名。wrapper 里的
> `exec` 不走 PATH 查找（沙箱子进程的 PATH 被 `--clean-env` 清空）。

#### 4.3.1 Bubblewrap 能否实现群聊级会话隔离？（对照数据）

**结论：能，且隔离强度与 sandlock 同级。** 实测（bwrap 0.9.0，`network=False` 默认）：

| 探测                            | 结果                                           | 判定                   |
| ------------------------------- | ---------------------------------------------- | ---------------------- |
| 写自己的目录                    | `exit=0`，文件落在工作区                       | ✅                      |
| **读其他会话目录**              | `cat: .../userB/their.txt: 没有那个文件或目录` | ✅ **群聊间隔离**       |
| **写其他会话目录**              | `cannot create .../userB/their.txt`            | ✅ **群聊间隔离**       |
| 读宿主 secret 文件              | `没有那个文件或目录`                           | ✅                      |
| 网络                            | `curl exit=6`（DNS 全断）                      | ✅                      |
| 私有 `/tmp`                     | 可写，宿主看不到                               | ✅                      |
| 写宿主文件                      | `cannot create`                                | ✅ 只读挂载             |
| **读 `/etc/passwd`**            | `root:x:0:0:root:/root:/bin/bash`              | ⚠️ 宿主全盘只读可见     |
| **读 bot 自己的 `config.yaml`** | 读出 `$schema` 等内容                          | ⚠️ **可读，含 API Key** |
| **读 bot 自己的源码**           | 读出 `[project] name = "kanade-bot"`           | ⚠️ 可读                 |
| **列宿主进程**                  | `systemd                                       | kthreadd               | ...` | ⚠️ 可见 |
| `network=True` 对比             | `curl exit=0`，577 bytes                       | 需显式开启             |

bwrap 实际 argv（源码 `_workspace.py:251`）：

```
bwrap --die-with-parent --new-session
     --unshare-user --unshare-ipc --unshare-uts --unshare-cgroup-try
     [--unshare-net --seccomp 3]        # network=False 时
     --cap-drop ALL
     --ro-bind / /   --dev /dev   --proc /proc   --tmpfs /tmp   --tmpfs /run
     --bind <working_dir> <working_dir>
     --chdir <working_dir>
```

源码注释解释了为何**不 unshare PID**：否则后台 job 会随调用消亡。代价是宿主进程可见且可 signal。

#### 4.3.2 关键风险：本项目的特殊性

**Bubblewrap 与 mirage/sandlock 有一个本质差异，对本项目是实质风险：**

|                      | Bubblewrap                   | mirage + sandlock                              |
| -------------------- | ---------------------------- | ---------------------------------------------- |
| 模型**能看见的目录** | **整个宿主文件系统（只读）** | **只有工作区**（VFS 隔离，其余路径直接不存在） |
| `/etc/passwd`        | 可读                         | 不可读                                         |
| bot 的 `config.yaml` | **可读**                     | 不可读                                         |

实测中，沙箱内的命令成功读出了 kanade-bot 的 `pyproject.toml` 和 `config.yaml`。对聊天机器人而言，**`config.yaml` / `config-prod.yaml` 里存着模型 API Key、平台 Token**——模型只要执行一次 `cat` 就能拿到，然后通过任何渠道外泄。

Bubblewrap 的设计目标是「防止误改、限制进程逃逸」，**不是防止信息读取**。加 `--tmpfs /etc` 之类的遮蔽能挡住 `config.yaml` 之类的敏感路径，但那是逐路径封堵，无法覆盖项目自己新增的敏感文件。

mirage 的 VFS 隔离是**默认安全**——工作区外的路径根本不存在，无需维护拒绝列表。

#### 4.3.3 多会话内存占用（三方案实测）

10 个会话并存，bot 进程 RSS 增量：

| 方案                       | 每会话常驻  | 说明                                                 |
| -------------------------- | ----------- | ---------------------------------------------------- |
| `LocalWorkspace`（无隔离） | **0.00 MB** | 纯 Python 对象                                       |
| `BubblewrapWorkspace`      | **0.00 MB** | bwrap 子进程短命，调用结束即回收；workspace 层无状态 |
| **mirage**                 | **0.10 MB** | 常驻 workspace 对象 + 内部缓存                       |

**三者常驻内存都可忽略**（bwrap 的开销在子进程上，短命且按需）。mirage 略高是因为它真的在进程内跑 VFS 缓存。

真正的差异不在内存，而在**文件操作是否经手虚拟文件系统**：

- mirage 的 `read_bytes`/`write_bytes` 走自己的 VFS，**路径全部受控**；
- Bubblewrap 是**宿主真实目录 + 只读挂载**，模型每条命令都是真的子进程调用。

#### 4.3.4 结论（已采纳）

**采用 mirage + sandlock。** 理由：

1. **隔离语义更适合本项目**——VFS 默认安全（工作区外路径不存在）vs bubblewrap 需逐路径封堵敏感文件；
2. 实测中 bubblewrap 可读 bot 的 `config.yaml`（含 API Key），而 mirage 不可；
3. mirage 已实测可用（23 项测试齐备），迁移只需加一层协议适配；
4. 内存差异 0.1 MB/会话，可忽略。

**Bubblewrap 在「进程逃逸」维度确实更强**（`--cap-drop ALL` + seccomp），但本项目的实际风险是
**模型读取 bot 自身配置**——这正是 bubblewrap 挡不住、mirage 天然挡住的维度。

若将来需要更强的进程逃逸防护（如允许模型装第三方包执行不受信代码），可再评估，
但那属于独立议题，不在本次迁移范围。

### 4.4 工具层：用 `PrepareTools` 精确复刻 `is_enabled`

**现有语义**：`@function_tool(is_enabled=_memory_enabled)` —— 关闭时工具**不注册进 schema**，模型完全看不到，省 token。

**正确迁移方案**：`PrepareTools` capability（`pydantic_ai.capabilities.PrepareTools`），它在**每次请求前**按 `ctx.deps` 过滤 `ToolDefinition`：

```python
from pydantic_ai.capabilities import PrepareTools
from pydantic_ai.tools import ToolDefinition

async def filter_tools(ctx: RunContext[ChatDeps], tool_defs: list[ToolDefinition]) -> list[ToolDefinition]:
    deps = ctx.deps
    return [t for t in tool_defs if t.name not in _conditionally_disabled(deps)]

agent = Agent(
    model,
    deps_type=ChatDeps,
    capabilities=[PrepareTools(filter_tools)],
)
```

**实测验证**（用 `FunctionModel` 拦截实际发出的 `info.function_tools`）：

```
memory_on=False: 实际发给模型的工具 = [['always']]
memory_on=True:  实际发给模型的工具 = [['memory_save', 'always']]
```

关闭时 `memory_save` **完全不出现在 schema 中** —— 与 `is_enabled` 语义一致。

**为什么不用其他方式**：

| 方案                                             | 是否达到 `is_enabled` | 说明                                             |
| ------------------------------------------------ | --------------------- | ------------------------------------------------ |
| 工具函数内首行 `if not enabled: return "不可用"` | ❌                     | 模型仍看到工具描述，白耗 token，还会反复尝试调用 |
| `FilteredToolset` 包裹                           | ✅                     | 可行但更重；`PrepareTools` 更直接                |
| **`PrepareTools`**                               | ✅                     | **官方推荐，代码最少**                           |

配套注意：
- `ToolDefinition.name` 是工具的对外名（本项目即函数名）；
- 过滤发生在压缩之后、记忆注入之前，需与 compaction capability 协调顺序（见 4.5.4）；
- `prepare_func` 不可被 spec 序列化（`get_serialization_name() -> None`），若将来用 Agent Spec 需注意。

`ChatContext` → `deps` 的字段映射：

| ChatContext 字段 | 迁后位置                                |
| ---------------- | --------------------------------------- |
| `session_info`   | `deps.session_info`                     |
| `bot_id`         | `deps.bot_id`                           |
| `memory_context` | `deps.memory_context`                   |
| `sandbox`        | `ctx.workspace`（不再走 deps）          |
| `sandbox_root`   | `deps.sandbox_root` 或由 workspace 提供 |

### 4.5 会话历史 / 压缩 / 记忆（三件事分开处理）
#### 4.5.1 会话历史存储 —— 自研 SQLite Session（**压缩事件落库，恢复完全一致**）

**关于 `StepPersistence`**：已确认**不符合**。它是 run 级恢复机制（每次 settled step 存快照 + 工具副作用账本），作用域 `conversation_id + run_id`，回答的是「上次崩了从哪继续」。

**核心设计目标（用户明确）**：**数据库保留全量消息；恢复时重建的历史必须与关闭前最后一次请求发送的内容完全一致**，以最大化 provider 侧前缀缓存命中率。

##### 4.5.1.1 关键实测结论

`ClearToolResults` 的重跑一致性（实测，`/tmp` 脚本可复现）：

```
[1] ModelMessagesTypeAdapter 序列化往返无损:        True
[2] 压缩 → 对压缩结果再压缩（幂等）:                True
[3] 从 DB 原始消息重跑压缩 → 与在线压缩结果一致:     True
[4] 多轮场景：轮1 清空 {c0,c1}，轮2 清空 {c0,c1,c2}
    从 DB 恢复重跑 → 清空 {c0,c1,c2}，与在线一致:   True
```

即 **`ClearToolResults` 是确定性 + 幂等的纯函数**：相同的输入历史 + 相同的 `keep_pairs` ⇒ 相同的输出。
因此**不需要把压缩后的完整内容再存一份**（避免双份存储），只需保证恢复时用**同样的输入 + 同样的参数**重跑。

##### 4.5.1.2 存储结构

```
┌─ messages 表（append-only，全量原始消息）──────────────────┐
│  session_id, seq, role, payload(JSON), ts                  │
│  ↳ 永不删除、永不覆盖 —— 唯一真相源，供审计与将来搜索        │
└────────────────────────────────────────────────────────────┘
              │ load() 全量
              ▼
┌─ compaction_marks 表（压缩事件标记，可空）────────────────┐
│  session_id, up_to_seq, strategy, params_json, applied_at  │
│  ↳ 记录「截至 seq=N 的消息已按 strategy+params 压缩过」    │
└────────────────────────────────────────────────────────────┘
              │
              ▼
┌─ 恢复：load() + 按 marks 重放 ────────────────────────────┐
│  history = 全量消息                                        │
│  for mark in marks: history = mark.strategy(history, params)│
│  ⇒ 结果 == 关闭前最后一次请求发送的内容                    │
└────────────────────────────────────────────────────────────┘
```

```python
# kanade_bot/utils/session_store.py
@dataclass
class CompactionMark:
    """一条压缩事件：标记「截至 up_to_seq 的消息已按 strategy/params 压缩」"""
    up_to_seq: int          # 压缩作用到的消息 seq（含）；None 表示对全量生效
    strategy: str           # 'clear_tool_results' | 'summarizing' | ...
    params: dict            # 如 {"keep_pairs": 3}
    applied_at: float


class SessionStore:
    """ModelMessage 全量持久化 + 压缩事件标记"""

    async def load(self, conv_id: str) -> list[ModelMessage]:
        """取全量原始历史（不含压缩）"""

    async def append(self, conv_id: str, msgs: list[ModelMessage]) -> None:
        """追加本轮新增消息（只追加 seq，不改动已有行）"""

    async def add_compaction_mark(
        self, conv_id: str, mark: CompactionMark
    ) -> None:
        """记录一次压缩事件（在线压缩发生后调用）"""

    async def load_compaction_marks(self, conv_id: str) -> list[CompactionMark]:
        """按 up_to_seq 升序返回该会话的压缩事件"""

    async def clear(self, conv_id: str) -> None:
        """reset：删除该会话的 messages 与 compaction_marks"""
```

##### 4.5.1.3 恢复流程

```python
async def restore_history(conv_id: str) -> list[ModelMessage]:
    """重建会话历史，结果与关闭前最后一次请求发送的内容一致"""
    history = await store.load(conv_id)            # 全量原始消息
    for mark in await store.load_compaction_marks(conv_id):
        history = apply_strategy(history, mark)    # 按记录重放
    return history
```

**为什么不存压缩后的完整内容**：
- `ClearToolResults` 确定性可重放（已实测），存标记即可；
- 避免双份存储（原始 + 压缩后各一份，后者只是把工具结果换成占位符，信息量重叠）；
- 压缩策略升级后，旧 marks 可选择性丢弃并重放新的策略（见 4.5.1.5）。

##### 4.5.1.4 ⚠️ 前提：压缩参数必须持久化且不可漂移

**实测发现的风险**：

```
keep_pairs=1 -> 清空 {c0,c1,c2}
keep_pairs=3 -> 清空 {c0}
```

**若恢复时用了不同的 `keep_pairs`，重建结果与关闭前不一致 → 前缀变化 → 缓存失效。**

因此：
1. **压缩参数必须记进 marks**（`params_json`），恢复时**用记录里的参数**，不用当前配置值；
2. **恢复路径必须在压缩参数变更时失效旧 marks**：实现上采取「参数指纹比对」——
   `marks` 存 `params_json`，若与当前策略参数不一致，则**丢弃旧 marks 并从全量重放新策略**。
   这在「改了配置」时损失一次缓存，但保证正确性；
   在「配置未变」（常态）时完全一致。

3. **在线压缩与恢复重放必须调用同一份代码**：把「应用某 strategy」抽成单一函数，
   在线压缩与恢复重放都调它，避免两套逻辑漂移。这是本设计的核心不变式。

##### 4.5.1.5 SummarizingCompaction 的特殊性（LLM 非确定性）

`SummarizingCompaction` 会调 LLM 生成摘要，**同一输入两次生成的文本不同**，无法靠重放复现。

处理方式（按 `min_clear_tokens` 未设 / 已触发摘要分级）：

| 情形 | 处理 |
| --- | --- |
| 只用了 `ClearToolResults`（常态） | 靠 marks 重放，完全一致 ✅ |
| 触发了 `SummarizingCompaction` | **必须把摘要内容存进 marks**（`params.result_messages`），恢复时直接用存下来的摘要，不重新生成 |
| — | 即 marks 分两类：可重放的（存参数）与不可重放的（存结果） |

**推论**：`marks` 表需要 `payload_json` 字段承载不可重放的摘要结果。

```python
@dataclass
class CompactionMark:
    up_to_seq: int | None
    strategy: str                    # 'clear_tool_results' | 'summarizing'
    params: dict                     # 可重放策略的参数（如 keep_pairs）
    result: list[dict] | None        # 不可重放策略的产物（摘要消息），可重放策略为 None
    applied_at: float
```

**结论**：常态（零成本档）完全一致；触发摘要时靠「存结果」保证一致。
两种情况都不会让前缀无故变化。

##### 4.5.1.6 其他要点

- 序列化用现成 `ModelMessagesTypeAdapter`（实测往返无损）；
- 存储粒度：一行一条 `ModelMessage`，便于按 seq 定位与将来做 `ConversationSearch`；
- `conv_id` 直接沿用现有 session_id 概念（用户/群 ID）；
- 现 `manager.py` 的 `pop_item()` 回退逻辑迁后改为 `list.pop()`——DB 是 append-only，
  回退只影响内存列表；但**已写入的 compaction mark 不回退**（压缩发生在请求前，
  与结果回退无关）；
- **DB 容量需关注**：全量保留意味着长期运行持续增长。建议加「按天/条数归档」
  （**本次不实现，记为待办**）。



#### 4.5.2 会话压缩 —— 采用 S3 `TieredCompaction`（**不用滑动窗口**）

**决策：S3** = `TieredCompaction([ClearToolResults, SummarizingCompaction], target_tokens=...)`。

**关键理由：本项目 KV Cache 命中率非常关键。**

滑动窗口每次裁掉最旧消息 → **请求前缀完全改变 → provider 侧前缀缓存全部失效**，
每轮都要重新 prefill 整个上下文，成本与延迟显著上升。

各策略对前缀缓存的影响：

| 策略 | 对前缀的影响 | 缓存友好度 |
| --- | --- | --- |
| `SlidingWindowCompaction` | **每轮都移动前缀起点** | ❌ **最差** |
| `ClearToolResults` | 清空旧工具结果内容，但**消息结构与位置不变** | ✅ 好（前缀稳定） |
| `SummarizingCompaction` | 旧消息替换为一条摘要，**仅超预算时发生** | ⚠️ 可接受（触发频率低） |
| **`TieredCompaction`** | 先跑零成本档，仍超预算才摘要 | ✅ **推荐** |

S3 的双重优势：**绝大多数轮次只做零成本的 `ClearToolResults`**（结构不变、前缀稳定），
只有真正超预算才付一次 LLM 做摘要（此时上下文已很长，缓存失效的相对代价小）。

完整策略菜单（均为 capability，请求前改写历史）：

| 策略 | LLM 成本 | 作用 |
| --- | --- | --- |
| `ClearToolResults` | 零 | 就地清空旧工具结果，保留最近 `keep_pairs` 对 |
| `DeduplicateFileReads` | 零 | 清空被新读取覆盖的旧文件读取 |
| `ClampOversizedMessages` | 零 | 截断单个超大 part（响应文本、工具参数） |
| `SummarizingCompaction` | 一次 LLM | 把旧消息摘要成结构化摘要 |
| `TieredCompaction` | 升级式 | 先跑便宜的，仍超 `target_tokens` 才摘要 |
| `FallbackCompaction` | 视链而定 | 某策略失败时降级 |
| ~~`SlidingWindowCompaction`~~ | 零 | **不采用**（前缀缓存不友好） |

**配对保证实测**（S3 的组成策略同样适用）：

```
输入 32 条（8 轮，含工具调用）-> 输出 6 条
  tool_call=1 tool_return=1
  孤儿 return: 无 / 孤儿 call: 无
  配对完整: True
```

官方原文：

> All strategies preserve tool-call / tool-return **pairing** — core does not
> validate this, and a provider rejects an orphaned pair.

其它设计优势：
- **编辑持久化进 run 历史**，不是每轮从全量重算；
- **`max_fraction` 按模型真实上下文窗口解析**，一个配置通用于所有模型（现有绝对 `max_tokens` 换个模型就不准）；
- **token 锚定最近一次 provider 报告的 usage**（`input_tokens` 是整个请求的测量值，只对之后新增的消息做差），比 4-chars-per-token 启发式准得多——对中文和 base64 内容尤其重要。

**迁移映射**：

| 现有配置 | 迁后 |
| --- | --- |
| `compaction_window_size=80`（items，滑动窗口） | **删除**（不再使用滑动窗口） |
| — | 新增：`compaction_target_tokens` 或 `compaction_max_fraction` |
| — | 新增：`compaction_keep_pairs`（`ClearToolResults` 保留对数，默认 3） |
| — | 新增：`compaction_min_clear_tokens`（见下，默认建议开启） |

> ⚠️ `compaction_window_size` 应**删除而非保留为 no-op**，避免留下「配了但不生效」的误导性配置。

**必须开启 `min_clear_tokens`（官方为此参数正是为了保护缓存）** —— `ClearToolResults`
docstring 原文：

> Cache tradeoff: clearing rewrites message content, which invalidates the
> provider's prompt cache **from the clear point onward** (the next request pays a
> cache-write). Use `min_clear_tokens` to skip clearing that reclaims too little
> to be worth busting the cache.

即：若这次清理只能省下很少 token，却要让整段前缀缓存失效，则**跳过这次清理**。
本项目既然把缓存命中率列为关键约束，应当启用它（建议初值 2k~4k token，现场调）。

**与 4.5.1 的衔接（实现责任）**：

在线压缩发生后，**必须把这次压缩写成一条 `CompactionMark` 落库**
（`up_to_seq` = 本轮结束时的最大 seq，`strategy` + `params`）。
恢复时按 marks 重放，即可复现关闭前的内容（见 4.5.1.3）。

实现上需要一个薄 wrapper capability 包住 `TieredCompaction`：

```python
class RecordingCompaction(AbstractCapability[ChatDepsT]):
    """调 TieredCompaction 做压缩，并把发生的压缩记进 SessionStore"""

    async def compact(self, ctx, messages):
        before = fingerprint(messages)
        after = await self._tiered.compact(ctx, messages)
        if fingerprint(after) != before:          # 确实发生了变化
            await self._store.add_compaction_mark(
                ctx.deps.conv_id,
                CompactionMark(
                    up_to_seq=await self._store.max_seq(ctx.deps.conv_id),
                    strategy=self._tiered_name,
                    params=self._current_params,   # 含 keep_pairs，供重放
                    result=None,                  # 零成本档可重放
                ),
            )
        return after
```

**不变式**：在线压缩与恢复重放**必须调用同一个 `apply_strategy` 函数**
（4.5.1.4 末条）——这是整个一致性保证的基础。

**生产观察项**（用户要求上线后观察）：
- 实际触发的压缩频率与摘要 LLM 调用量；
- provider 侧缓存命中率是否因压缩下降；
- 超长会话（数千轮）的上下文质量是否退化。

若观察到 `ClearToolResults` 不够（工具输出之外的对话本身也超预算），
可考虑调低 `keep_pairs`，或引入 `DeduplicateFileReads`（若模型反复读文件）。


#### 4.5.3 记忆 —— **本次迁移不改动**（已决策）

**决策：保持现有 `MemoryStore` 原样。** 记忆系统后续会整体重写，本次不参与。

迁移期间只需做**最小适配**：`memory.py` 的三个工具（`save_memory` / `recall_memory` /
`forget_memory`）从 `@function_tool` 改为 `@agent.tool`，上下文访问从
`RunContextWrapper[ChatContext].context` 改为 `RunContext[ChatDeps].deps`。
**存储逻辑、作用域隔离、检索方式一律不动。**

条件启用从 `@function_tool(is_enabled=_memory_enabled)` 改为 `PrepareTools`（见 4.4），
保持「无记忆范围时工具不注册到 schema」的原有语义。

> 为什么不在本次一起改：记忆系统本身还有已知待办（见 `MEMORY_DESIGN.md`：
> 「生产中模型很少主动调用 `save_memory`」、缺被动预检索）。若在同一次迁移里
> 既换 agent 框架又换记忆实现，出问题将难以定位是框架迁移导致还是记忆改造导致。
> **记忆重写作为独立任务，另行安排。**

**（存档）Pydantic AI Harness `Memory` 的设计要点**，供未来重写记忆系统时参考：

- 形态是 **Markdown 笔记本**：`MEMORY.md` 主笔记本 + 其他聚焦笔记；
- 4 个工具：`write_memory` / `read_memory` / `delete_memory` / `search_memory`；
- 注入方式：主笔记本的**有界摘录** + 其他文件名，作为带 `<memory>` 分隔符的
  user-role part 注入**当前请求**（不累积进历史）；
- `max_tokens` 默认 2000（估算），`max_lines` 附加限制；
- **namespace 由应用代码解析，不由工具参数决定** —— 官方原文：
  > The namespace is resolved by application code, not supplied to the tools. The
  > model therefore cannot select another user's namespace in a tool call.

与现有 `MemoryStore` 对照：作用域隔离**同等安全**（官方方案这点设计更明确，
现有实现靠工具绑定 `ChatDeps` 也满足）；但官方是 Markdown 文件形态，
会丢掉现有的结构化存储与「同主题更新」能力。未来重写时需权衡。


#### 4.5.4 capability 注册顺序

官方明确了一个易踩的坑：

> List request-only injectors such as `Memory` after compaction so they apply to the
> compacted request. Earlier request-only edits are discarded.

即 capability 顺序应为：**压缩 → 工具过滤 → 其它注入**。
本次迁移涉及两个：`TieredCompaction`（压缩）+ `PrepareTools`（工具过滤）。
**注册顺序需固定并加注释**，否则会出现「工具过滤被压缩丢弃」或反之。

由于本次**不改记忆系统**，不涉及第三方注入器，复杂度较低。

### 4.6 MCP

```python
# 现
MCPServerStreamableHttp(params=MCPServerStreamableHttpParams(url=...), name=..., tool_filter=...)

# 迁（需 pydantic-ai-slim[mcp]，底层换成 fastmcp）
from pydantic_ai.mcp import MCPToolset
toolset = MCPToolset(url=..., tool_filter=...)
agent = Agent(..., toolsets=[toolset])
```

**差异**：MCP server 成为 **toolset** 而非挂到 agent 上；`drop_failed_servers` 语义需自行处理。

### 4.7 其他

| 项                       | 说明                                                           |
| ------------------------ | -------------------------------------------------------------- |
| summary 插件             | 只用 `from agents import Agent`，改动最小                      |
| image_caption            | 只用 `Runner.run`，小改                                        |
| RAG                      | 与 agent 框架解耦，不受影响                                    |
| crystal / fun 等业务插件 | 不受影响                                                       |
| 配置 schema              | `BaseAgentConfig` 需加 Pydantic AI 特有项（`usage_limits` 等） |
| `openai-proxy`           | 已移除，无影响                                                 |

---

## 5. 实施阶段（每阶段可独立上线/回滚）

### 阶段 0：可行性验证

在真实 DeepSeek 端点上跑通最小脚本，确认：
1. `OpenAIChatModel` + DeepSeek 的 `reasoning_effort` / thinking 如何映射；
2. 流式 + 工具调用 + `finish_reason` 在真实模型上的行为；
3. `PrepareTools` 在真实请求中确实过滤 schema。

**产出**：`spike_pai.py`，确认无阻塞项再进入阶段一。

### 阶段 1：模型层 + 工具层 ⭐ 风险最低

1. `utils/agents_runtime.py` → `utils/pai_runtime.py`（**删 100+ 行 hack**）；
2. `tool.py` 全部工具改 `@agent.tool` + `RunContext[ChatDeps]`；
3. **条件工具用 `PrepareTools`**（保持 `is_enabled` 语义，见 4.4）；
4. `context.py` → `deps.py`；
5. `manager.py` 最小可用流式（文本 + 工具调用），**暂不迁 watchdog/空响应/续写**；
6. summary / image_caption 同步迁移（**全量，不保留 openai-agents**）。

**不动**：会话历史（先纯内存 `list[ModelMessage]`）、沙箱（先不开）。

**验证**：多轮对话、工具调用、图片输入、流式输出、MCP 连接、关闭的工具确实不入 schema、与迁移前的主观质量对比。

### 阶段 2：会话历史 + 压缩

1. `SessionStore`（SQLite + `ModelMessagesTypeAdapter`），**append-only、DB 保留全量** + **compaction_marks 表**（见 4.5.1）；
2. **抽出唯一的 `apply_strategy` 函数**（在线压缩与恢复重放共用——这是缓存一致性的基础不变式）；
3. **压缩换用 S3 `TieredCompaction`** + `min_clear_tokens`（见 4.5.2），删除 `openai-agents-context-compaction` 依赖与 `compaction_window_size` 配置项；
4. `RecordingCompaction` wrapper：在线压缩后写 mark；
5. 补齐 watchdog / 空响应回退 / 截断续写；
6. 消息缓冲区缓存格式迁移（旧 JSON 缓存作废）。

**验证**：
- 重启后会话恢复、**工具调用对裁剪后零孤儿**、长会话内存可控；
- **DB 中未被压缩的消息仍完整保留**（直接查库核对）；
- **一致性关键测试**：记录「关闭前最后一次请求发送的消息指纹」→ 重启 →
  重建历史指纹**必须相同**（本阶段的核心验收项）；
- **参数漂移测试**：改 `keep_pairs` 后恢复，按 4.5.1.4 丢弃旧 marks 并重放，结果自洽；
- 与迁移前的上下文质量对比。

> 压缩策略是配置项，验证时可调 `target_tokens` / `keep_pairs` / `min_clear_tokens` 观察不同阈值下的行为。

### 阶段 3：沙箱

1. `MirageBackend(WorkspaceBackend)` 实现协议方法（`run`/`read_bytes`/`write_bytes`/`list_dir` 等）；
2. 通过 `WorkspaceBackendSuite` 一致性测试；
3. 自写 workspace toolset（`execute` 工具调 `ctx.workspace.run`）；
4. `ChatDeps.sandbox` 移除，工具改用 `ctx.workspace`；
5. `render_html_image` / `send_file` / `send_image` 改走 workspace；
6. **Landlock ABI 降级 shim**（生产内核 6.8 = ABI v4，见 4.3.0.1）：启动时 `sandlock check` 读 ABI，不足 v6 则启用 wrapper 注入 `--allow-degraded signal-scope --allow-degraded abstract-unix-socket-scope --allow-degraded fs-ioctl-dev`。
   启动时 `sandlock check` 读 ABI，不足 v6 则启用 wrapper 注入
   `--allow-degraded signal-scope --allow-degraded abstract-unix-socket-scope
   --allow-degraded fs-ioctl-dev`。

**验证**：复用现有 `tests/chat/test_mirage_sandbox.py`（23 项）+ 新增协议一致性测试；**重点回归隔离边界**（未授权路径读写被拒、secret 不可读）、TTL/LRU、内存占用。

### 阶段 4：清理

- 删除 `openai-agents`、`openai-agents-context-compaction` 依赖及全部引用；
- 依赖收敛：`pydantic-ai-slim[mcp]` + `pydantic-ai-harness`；
- 文档/schema/依赖更新；
- 回归全部 SUPERUSER 命令。

**阶段顺序说明**：沙箱（阶段 3）放在会话历史（阶段 2）之后，理由是会话历史是更核心的链路（每天都在用），先把它做稳；沙箱依赖 workspace 抽象，而该抽象在阶段 1 的工具迁移中已经铺好。

### 各阶段对应的决策落地

| 阶段 | 落地的决策 |
| --- | --- |
| 1 | 决策 1（全量迁移）；4.4 `PrepareTools`；4.5.3 记忆**最小适配**（存储不动） |
| 2 | 决策 3（自研 SessionStore，**DB 全量**）；决策 4（`TieredCompaction`，**删滑动窗口配置**） |
| 3 | 决策 2（mirage 包装 `WorkspaceBackend`） |
| 4 | 删除 `openai-agents` / `openai-agents-context-compaction` 全部依赖与引用 |

---

## 6. 风险与缓解

| #   | 风险                                                             | 等级 | 缓解                                                                                                          |
| --- | ---------------------------------------------------------------- | ---- | ------------------------------------------------------------------------------------------------------------- |
| P1  | **Pydantic AI 更年轻**，`Workspace` / Harness 是新抽象，可能演进 | 中高 | 沙箱层隔离在 `sandbox.py` 单文件；`WorkspaceBackendSuite` 能提前发现接口变化                                  |
| P2  | **会话历史要自研**                                               | 中   | 序列化用现成 `ModelMessagesTypeAdapter`；配对正确性由官方压缩策略保证，实际比现有实现更简单                  |
| P3  | **流式事件模型不同**，watchdog/空响应/续写要重写                 | 中   | 阶段一先最小可用，逐个补齐；每补一个跑回归                                                                    |
| P4  | **DeepSeek 的 reasoning/thinking 映射**不确定                    | 中   | 阶段 0 专项验证；若不支持则该能力降级（不阻塞迁移）                                                           |
| P5  | **MCP 换 fastmcp 客户端**，行为差异                              | 低   | tavily/anysearch 均为标准 Streamable HTTP；阶段一验证                                                         |
| P6  | **Harness 0.x API 可能在小版本间变化**                           | 中   | 官方明说会通过 deprecation warning 指引迁移；把 Harness 用法集中在沙箱与压缩两处                              |
| P7  | **capability 注册顺序**坑（压缩 vs 工具过滤）                    | 低   | 见 4.5.4，在实现时固定顺序并加注释                                                                            |
| P8  | `usage_limits` 默认 50 对长工具链可能偏紧                        | 低   | 显式配置 `UsageLimits(request_limit=...)`                                                                     |
| P9  | **DB 全量保留导致容量持续增长**                                  | 低   | 本次不实现归档，记为待办；上线后按实际增速评估                                                                 |
| P10 | **压缩参数变更导致恢复后前缀不一致**（缓存失效）               | 中   | 参数记进 marks；恢复用记录值而非当前配置；参数漂移时丢弃旧 marks 重放（见 4.5.1.4）                            |
| P11 | **在线压缩与恢复重放逻辑漂移**（两套代码不一致）                | 中高 | **强制共用同一个 `apply_strategy` 函数**——这是缓存一致性的基础不变式（见 4.5.1.4）                            |
| P12 | `SummarizingCompaction` 非确定性，摘要无法重放                  | 中   | 摘要结果存进 mark 的 `result` 字段，恢复时直接用（见 4.5.1.5）                                               |
| P13 | **生产内核 6.8 只有 Landlock ABI v4**，sandlock 默认 Strict 会拒绝启动 | 高 | **已定方案**：wrapper 注入 `--allow-degraded`（见 4.3.0.1）；启动自检 `sandlock check`，ABI < v4 才报错 |
| P14 | **降级后失去 SignalScope / AbstractUnixSocketScope / FsIoctlDev** | 中   | 已在 4.3.0.1 列明影响；**文件系统隔离主线不受影响**（靠 Landlock FS rules + mirage VFS）；运维文档需告知 |
| P13 | **生产内核 6.8 只有 Landlock ABI v4**，sandlock 默认 Strict 会拒绝启动 | 高 | **已定方案**：wrapper 注入 `--allow-degraded`（见 4.3.0.1）；启动自检 `sandlock check`，ABI < v4 才报错 |
| P14 | **降级后失去 SignalScope / AbstractUnixSocketScope / FsIoctlDev** | 中 | 已在 4.3.0.1 列明影响；**文件系统隔离主线不受影响**（靠 Landlock FS rules + mirage VFS）；运维文档需告知 |

---

## 7. 决策记录（全部已定）

### 7.1 最终决策（2026-10-03）

| #   | 事项                     | 决策                                                                                     |
| --- | ------------------------ | ---------------------------------------------------------------------------------------- |
| 1   | 是否值得迁移             | **值得**                                                                                 |
| 2 | 沙箱方案 | **mirage + sandlock**，包装为 `WorkspaceBackend`；**不用 bubblewrap**（实测可读到 bot 的 `config.yaml`，含 API Key）；**生产内核 6.8（ABI v4）需对 v6 protection 显式降级**（见 4.3.0.1） |
| 3   | 是否保留 openai-agents   | **全量迁移，不再使用**（summary / image_caption 一并迁）                                |
| 4   | 会话历史                 | **自研 SQLite Session**：DB **append-only 保留全量** + **compaction_marks 表**；恢复时按 marks 重放，保证与关闭前一致 |
| 5   | 会话压缩                 | **S3 `TieredCompaction`**（`ClearToolResults` → `SummarizingCompaction`）；**不用滑动窗口**（KV Cache 命中率关键）；**必开 `min_clear_tokens`**。生产中观察后再调 |
| 6   | 记忆系统                 | **本次不改动**，仅做最小适配（`@function_tool` → `@agent.tool`）。后续单独重写           |
| 7   | 有界自动注入（Q2）       | **选 A**——本次不做                                                                 |
| 8   | 阶段顺序                 | 0 → 1 → 2 → 3 → 4                                                                       |

### 7.2 实现时必须遵守的约束

1. **capability 注册顺序**：压缩 → 工具过滤（见 4.5.4）；
2. **`SessionStore` 是 append-only**：原始消息永不修改；压缩结果**不回写 messages 表**，
   只以 `compaction_marks` 记录「压缩过哪里」；
3. **在线压缩与恢复重放必须共用同一个 `apply_strategy` 函数** —— 两套代码必然漂移，
   一旦漂移缓存就失效（这是本设计最重要的不变式）；
4. **压缩参数记进 marks**，恢复时用记录值，不用当前配置；参数漂移时丢弃旧 marks 重放；
5. **`SummarizingCompaction` 的摘要结果必须存进 mark**，恢复时直接用，不重新生成；
6. **开启 `min_clear_tokens`**：清理收益太小则跳过，避免白白弄坏缓存；
7. **`compaction_window_size` 配置项删除**（不再使用滑动窗口），不留 no-op 配置；
8. **沙箱隔离边界必须回归测试**：模型读不到 bot 的 `config.yaml` / 源码 / `.env`；
9. **Landlock ABI 必须显式降级**：生产内核 6.8 = ABI v4 < v6，sandlock 默认 Strict
   会拒绝启动。启动时 `sandlock check` 读 ABI，不足则启用 wrapper 注入
   `--allow-degraded`；ABI < v4（连文件系统规则都保不住）才报错（见 4.3.0.1）；
10. **记忆存储逻辑不动**：只换装饰器与上下文访问方式，不碰 `MemoryStore` 的 schema、
    作用域隔离、检索方式；
11. **不改 `memory.py` 的数据结构**——记忆系统后续会整体重写，本次不参与；
12. `PYDANTIC_AI_NO_BANNER=1`：避免启动时打 ASCII banner；
13. `UsageLimits` 显式配置（默认 50 对长工具链偏紧）。

### 7.3 上线后观察项

- **压缩频率与摘要 LLM 调用量**（`SummarizingCompaction` 触发次数）；
- **provider 侧缓存命中率**是否因压缩下降（这是选 S3 而非滑动窗口的核心依据）；
- **重启后首次请求是否命中缓存**（验证 marks 重放机制有效）；
- `min_clear_tokens` 是否设得合理（观察被跳过的清理次数）；
- 超长会话（数千轮）的上下文质量是否退化；
- DB 增长速度（全量保留 + marks）。

---

## 8. 附录：实测验证清单

全部在 `.venv`（`pydantic-ai-slim==2.54.0` + `pydantic-ai-harness==0.54.0`）实跑，非文档推断。

### 8.1 模型层与工具

| 验证点                               | 方法                                       | 结果                                             |
| ------------------------------------ | ------------------------------------------ | ------------------------------------------------ |
| Chat Completions 端到端              | 假 server + `OpenAIChatModel`              | ✅                                                |
| 流式文本                             | `run_stream()` + `stream_text(delta=True)` | ✅                                                |
| **原生 `finish_reason`**             | server 返 `finish_reason="length"`         | ✅ `all_messages()[-1].finish_reason == 'length'` |
| 工具调用循环                         | server 先返 tool_calls 再返文本            | ✅ `ToolCallPart` → `ToolReturnPart`              |
| **`PrepareTools` 等价 `is_enabled`** | `FunctionModel` 拦截 `info.function_tools` | ✅ 关闭时工具**不入 schema**                      |
| 用量统计                             | `RunUsage`                                 | ✅ 注意是**属性**非方法                           |
| 内置限额                             | 默认 `request_limit=50`                    | ✅ 超限抛 `UsageLimitExceeded`                    |

### 8.2 压缩

| 验证点                         | 方法                      | 结果         |
| ------------------------------ | ------------------------- | ------------ |
| `SlidingWindowCompaction` 生效 | 32 条（8 轮含工具）→ 压缩 | ✅ 输出 6 条  |
| **工具调用对配对**             | 校验孤儿 call/return      | ✅ **零孤儿** |

### 8.3 沙箱隔离与内存

Bubblewrap（`bwrap` 0.9.0，`network=False`）：

| 探测                               | 结果                                    |
| ---------------------------------- | --------------------------------------- |
| 写自己目录                         | ✅ `exit=0`                              |
| **读/写其他会话目录**              | ✅ **均被拒**                            |
| 读宿主 secret 文件                 | ✅ 被拒                                  |
| 网络                               | ✅ `exit=6`（全断）；`network=True` 恢复 |
| 写宿主文件                         | ✅ 被拒（只读挂载）                      |
| 读 `/etc/passwd`                   | ⚠️ 可读                                  |
| **读 bot 的 `config.yaml` / 源码** | ⚠️ **可读（含 API Key）**                |
| 列宿主进程                         | ⚠️ 可见（故意不 unshare PID）            |

内存（10 会话并存，bot 进程 RSS 增量）：

| 方案                  | 每会话  |
| --------------------- | ------- |
| `LocalWorkspace`      | 0.00 MB |
| `BubblewrapWorkspace` | 0.00 MB |
| mirage                | 0.10 MB |

---

## 9. 与既有计划的关系

- `MIGRATION_PLAN.md`（Copilot SDK → openai-agents）：**已完成**，本文档是其后续。
- 本文档**不改写**既有计划，只在其之上规划二次迁移。
- mirage 沙箱方案（`MIGRATION_PLAN.md` 1.4 节）在本文档的 4.3 中**继续有效**，只需包装为 `WorkspaceBackend`；
  若改选 bubblewrap，需重写该节并接受 4.3.4 的封堵维护负担。
