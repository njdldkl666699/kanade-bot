# 执行任务：迁移 kanade-bot 从 openai-agents 到 Pydantic AI

> 完整技术方案见本仓库 `MIGRATION_PLAN_PYDANTIC_AI.md`（**已定案，按它执行**）
> 本文件是新会话的入口提示词。

---

## 背景

kanade-bot 是一个 NoneBot2 聊天机器人，chat 主 agent 当前用 `openai-agents` SDK
（0.23.1）**通过 Chat Completions API** 调用 deepseek 官方 API。

因为 SDK 的 sandbox / apply_patch 等新能力优先甚至仅面向 Responses API，本项目
被迫放弃 `Filesystem` capability（`apply_patch` 是 FREEFORM/grammar 工具，
Chat Completions 下会在工具转换时抛 `UserError`）。

现在要**全量迁移到 Pydantic AI**，它把 Chat Completions（`OpenAIChatModel`）和
Responses（`OpenAIResponsesModel`）做成一等公民的对等模型。

沙箱已从 Docker 换成 **mirage + sandlock**（进程内虚拟文件系统，~0.1MB/会话），
本次迁移需要把它包装成 Pydantic AI 的 `WorkspaceBackend`。

## 必读（按顺序，不要跳）

### 1. 先读官方迁移技能包 —— 最重要

pydantic_ai 包**自带官方迁移技能包**，是维护者视角的权威指引，比任何在线文档都准：

```
.venv/lib/python3.14/site-packages/pydantic_ai/.agents/skills/migrating-openai-agents-sdk-to-pydantic-ai/
├── SKILL.md                                  ← 必读
├── references/CONCEPT-MAPPING.md             ← 概念对照表（94 行）
└── references/VERIFICATION-AND-CUTOVER.md    ← 改持久化/流式/安全前必读（88 行）
```

SKILL.md 里有几条与本项目直接相关的指导，务必注意：

- **"Preserve caller-visible behavior, not SDK object shapes"** —— 保行为，不保对象形状
- **"Migrate the smallest complete runtime slice and leave application infrastructure in place"**
- 沙箱：**"a shell allowlist is not isolation"**，执行环境要单独选
- 流式：`run(event_stream_handler=...)` / `run_stream_events()` / `iter()` 用于完整
  agent 循环；`run_stream()` 只适用于「提交首个匹配输出即结束」的场景。
  **本项目 chat 走完整循环（含工具调用后继续生成），不要用错**
- `UsageLimits.request_limit` 要在**验证了计数与失败契约之后**再继承（默认 50 不同）

### 2. 再读项目记忆

`/memories/repo/` 下已有多份本次调研的实测结论，**全部是实跑验证过的**：

| 文件 | 内容 |
| --- | --- |
| `sandlock-abi-degradation.md` | **生产 6.8 内核 ABI 降级方案**（本轮新增）：ABI 下限表、CLI 降级手段、shim 实现、安全影响 |
| `session-compaction-design.md` | **压缩事件落库方案**（本轮核心）：实测结论、两个前提、官方 `min_clear_tokens` |
| `pydantic-ai-eval.md` | Pydantic AI 能力矩阵、坑、官方三件套方案、bubblewrap 实测 |
| `pydantic-ai-skill.md` | 官方技能包位置与关键指导 |
| `mirage-main-vs-pypi.md` | 为什么 mirage 用 git main 而非 PyPI（含 PyPI 版三个缺陷） |
| `mirage-sandbox.md` | mirage 沙箱的行为细节与已知限制 |
| `agents-sdk.md` / `copilot-sdk.md` | 前两次迁移的记录 |

### 3. 最后读计划

`MIGRATION_PLAN_PYDANTIC_AI.md`，重点是：
- 第 3 节：现有实现盘点与 API 映射表、可直接删除的清单
- 第 4 节：各分项设计决策（模型层 / 流式 / 沙箱 / 工具层 / 会话历史压缩记忆）
- 第 5 节：五个实施阶段
- 第 7.2 节：**实现时必须遵守的约束**

## 已定方案（不要重新讨论）

| # | 事项 | 决策 |
| --- | --- | --- |
| 1 | 迁移范围 | **全量迁移**，不再保留 openai-agents（summary / image_caption 一并迁） |
| 2 | 沙箱 | **mirage + sandlock** 包装为 `WorkspaceBackend`；**不用 bubblewrap**；**生产内核 6.8（ABI v4）需对 v6 protection 显式降级** |
| 3 | 会话历史 | **自研 SQLite Session**：DB **append-only 保留全量** + **compaction_marks 表**；恢复时按 marks 重放，保证与关闭前一致（保护 KV Cache） |
| 4 | 会话压缩 | **`TieredCompaction([ClearToolResults, SummarizingCompaction])`**；**不用滑动窗口**；**必开 `min_clear_tokens`** |
| 5 | 记忆系统 | **本次不改动**，仅把 `@function_tool` 换成 `@agent.tool` |
| 6 | 条件工具启用 | 用 **`PrepareTools`** capability（保持 `is_enabled` 语义） |
| 7 | 阶段顺序 | 0 → 1 → 2 → 3 → 4，每阶段可独立上线/回滚 |

### 为什么不用滑动窗口（决策 4 的关键）

**本项目 KV Cache 命中率非常关键。** 滑动窗口每轮移动前缀起点 → provider 侧
前缀缓存全部失效 → 每轮重新 prefill 整个上下文。`ClearToolResults` 只清空旧工具
结果的**内容**，消息结构与位置不变，前缀稳定。

### 为什么不用 bubblewrap（决策 2 的关键）

实测发现 bwrap 用 `--ro-bind / /`，沙箱内命令能直接 `cat` 出 bot 自己的
`pyproject.toml` 和 **`config.yaml`（含 API Key / 平台 Token）**。bubblewrap 防的是
「误改 + 进程逃逸」，不防信息读取。mirage 的 VFS 隔离是默认安全——工作区外的路径
根本不存在，无需维护拒绝列表。

### 为什么压缩事件要落库（决策 3 的关键）

需求：**恢复时重建的历史必须与关闭前一致**。实测 `ClearToolResults` 是确定性 +
幂等的纯函数（同输入同参数 ⇒ 同输出），所以**只需记录「压缩过哪里 + 用的什么
参数」**（compaction_marks），恢复时重放即可，无需存压缩后的完整内容。

但有两个前提必须满足，否则一致性破功：
- **参数不能漂移**（实测 keep_pairs 从 1 改 3，清空集合就不同）；
- **在线压缩与恢复重放必须共用同一个函数**。

## 实施阶段

按 `MIGRATION_PLAN_PYDANTIC_AI.md` 第 5 节执行。阶段概览：

- **阶段 0（0.5 天）**：真实 DeepSeek 端点验证 `reasoning_effort`/thinking 映射
- **阶段 1（3-5 天）**：模型层 + 工具层。删掉 `agents_runtime.py` 里约 100 行的
  `LengthTrackedChatCompletionsModel` + ContextVar hack（`finish_reason` 是原生字段）
- **阶段 2（3-5 天）**：`SessionStore`（全量存储）+ S3 压缩
- **阶段 3（3-5 天）**：mirage → `WorkspaceBackend`
- **阶段 4（1 天）**：清理 `openai-agents` 依赖

## 环境准备

```bash
# 依赖
uv sync

# 必须装 sandlock CLI（沙箱依赖它）
#   https://github.com/multikernel/sandlock/releases
bwrap --version   # 已装（本项目不用，但确认一下环境）

# git 依赖需代理（mirage 走 git main）
git config --global http.proxy http://127.0.0.1:10808

# 设这个避免启动时打 ASCII banner
export PYDANTIC_AI_NO_BANNER=1
```

### ⚠️ 生产环境：内核 6.8 → Landlock ABI v4 必须降级

**生产内核是 6.8，Landlock 只有 ABI v4**，而 sandlock 要求 **ABI v6**（6.12+）。
它默认是 **Strict**：required protection 不可用时**直接拒绝启动**，沙箱会不可用。

启动时先 `sandlock check` 看实际 ABI（它会报 `Landlock: ABI vN` 与
`Minimum required: ABI v6`），不足则启用 wrapper 注入：

```bash
--allow-degraded signal-scope
--allow-degraded abstract-unix-socket-scope
--allow-degraded fs-ioctl-dev
```

这 3 项对应 ABI v5/v6，6.8 上确实没有；其余（`FsRefer`/`FsTruncate`/`NetTcp`/
文件系统规则）在 ABI v4 上都生效。

**关键：mirage 的 `SandlockRuntime` 硬编码 argv，没有透传接口**，所以要用
PATH shim：写一个 `sandlock` wrapper 放到 PATH 前面，在 `--` 之前插入
`--allow-degraded` 参数后 exec 真正的 sandlock。（mirage 用
`shutil.which("sandlock")` 查找，会命中 wrapper。）

**本机 sandlock 实际在 `/home/cola/Softwares/sandlock/sandlock`**（不在
`/usr/local/bin`），由 `~/.profile` 注入 PATH。手动从交互式 shell 启动 bot 时
PATH 正常继承（已实测），所以配置里 `landlock_real_binary` 留空即可——生成
wrapper 时用 `shutil.which("sandlock")` 动态求值，不要硬编码路径。

**两个必须避开的坑**：

| 坑 | 现象 | 正解 |
| --- | --- | --- |
| `${@:2}` | bash 扩展，`/bin/sh`(dash) 不支持 → `Bad substitution`，**wrapper 静默失效** | `sub="$1"; shift` 后用 `"$@"` |
| 硬编码路径 | 换机器就找不到 | `shutil.which()` 动态求值 |

wrapper 生成后要**自检**：用 wrapper 跑一条带 v6 protection 的命令，确认没报
`protection unavailable`。否则降级没生效却看不出来，是最危险的失败模式。

**降级后仍然保留的（重要）**：文件系统隔离（Landlock FS rules + mirage VFS）、
内存限额。即 mirage 的「工作区外路径不存在」这条隔离主线**不受影响**。
失去的是防进程逃逸的三项：signal 宿主进程、连 abstract unix socket、设备 ioctl。

**降级策略分档**：
- ABI ≥ v6 → 用真 sandlock，无需 wrapper；
- v4 ≤ ABI < v6 → 启用 wrapper，并在启动日志里明确打印失去的保护；
- ABI < v4 或 Landlock 不可用 → 报错（此时连文件系统隔离都保不住）。

详见计划 4.3.0.1。

**依赖清单**（阶段 4 会收敛）：
- 删：`openai-agents`、`openai-agents-context-compaction`
- 留：`mirage-ai`（git main，`#subdirectory=python`，见 `mirage-main-vs-pypi.md`）
- 加：`pydantic-ai-slim[mcp]`、`pydantic-ai-harness`

## 硬性约束（违反会导致返工）

1. **capability 注册顺序**：压缩 → 工具过滤。顺序错会导致「工具过滤被压缩丢弃」。
2. **`SessionStore` 必须 append-only**：原始消息永不修改；压缩结果**不回写
   `messages` 表**，只以 `compaction_marks` 记录「压缩过哪里」。
3. **在线压缩与恢复重放必须共用同一个 `apply_strategy` 函数** —— 这是本设计
   最重要的不变式。两套代码必然漂移，一漂移前缀就变、缓存就失效。
4. **压缩参数记进 marks**，恢复时用记录值而非当前配置；参数变更时丢弃旧 marks
   并从全量重放（实测 `keep_pairs` 从 1 改 3 会导致清空集合不同）。
5. **`SummarizingCompaction` 的摘要结果必须存进 mark**，恢复时直接用、不重新
   生成（LLM 非确定性，无法重放）。
6. **开启 `min_clear_tokens`**：清理收益太小则跳过，避免白白弄坏缓存。
7. **`compaction_window_size` 配置项要删除**，不要留成 no-op（避免「配了但不生效」）。
8. **沙箱隔离边界必须回归测试**：模型读不到 bot 的 `config.yaml` / 源码 / `.env`。
   现有 `tests/chat/test_mirage_sandbox.py`（23 项）要继续通过。
9. **Landlock ABI 必须显式降级**（生产 6.8 = ABI v4 < v6）：sandlock 默认 Strict 会
   拒绝启动。启动时 `sandlock check` 读 ABI，不足则启用 PATH wrapper 注入
   `--allow-degraded`；**ABI < v4 才报错**（那时连文件系统隔离都保不住）。
10. **记忆存储逻辑不动**：只换装饰器与上下文访问方式，不碰 `MemoryStore` 的
   schema、作用域隔离、检索方式。
11. **不改 `memory.py` 的数据结构**——记忆系统后续会整体重写，本次不参与。

## 阶段 2 的核心验收项

**恢复一致性测试**（本阶段最重要）：

1. 记录「关闭前最后一次请求实际发送的消息指纹」；
2. 重启 bot；
3. 重建历史，计算指纹；
4. **两者必须完全相同**。

另需测「参数漂移」：改 `keep_pairs` 后恢复，验证按规则丢弃旧 marks 并重放，结果自洽。

## 验证要求

每个阶段结束前跑：
- `tests/chat/test_mirage_sandbox.py`（23 项，隔离边界）
- ruff check + format
- 阶段特定的端到端检查（见计划各阶段）

阶段 2 额外要**直接查库核对**：确认 DB 里被压缩掉的消息仍然完整保留。

## 已知坑（都实测过）

| 坑 | 说明 |
| --- | --- |
| MCP 需 `pydantic-ai-slim[mcp]` | 底层是 fastmcp 不是 mcp SDK；类名是 `MCPToolset` |
| `result.usage` 是**属性** | 不是方法，`r.usage.input_tokens` |
| `UsageLimits(request_limit=50)` 默认 | 长工具链可能偏紧，需显式配置 |
| `WorkspaceBackendSuite` | 官方一致性测试套件，写自定义 backend 时用它 |
| Harness API 0.x | 小版本间可能变；官方说会用 deprecation warning 指引 |
| mirage 模块路径 | 用 `mirage.vfs.disk.DiskVFS`、`mirage.runtime.sandbox.sandlock` |
| 工作区必须 `realpath()` | `/tmp` 是 symlink，不解析会写进 workspace overlay |
| `MountMode` 必须 `EXEC` | 用 `WRITE` 时 python3 报 `not in EXEC mode`（exit 126） |

## 收尾

阶段 4 完成后：
- 更新 `README.md`（沙箱部署说明里的依赖部分）
- 更新 `MIGRATION_PLAN_PYDANTIC_AI.md` 的完成状态
- 重新生成 `schemas/*.json`（`uv run python bot.py`，注意会临时改 `bot.py` 的
  `nonebot.run()`，生成后要改回来——这个坑之前踩过）
- 回归全部 SUPERUSER 命令
- 把本次迁移的新结论写进 `/memories/repo/`（尤其是任何与计划不符的发现）

## 待观察（上线后）

- 压缩触发频率与摘要 LLM 调用量
- provider 侧缓存命中率是否因压缩下降（这是选 S3 的核心依据）
- 超长会话的上下文质量
- DB 增长速度（全量保留）