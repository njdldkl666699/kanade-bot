# AGENTS.md — kanade-bot 开发指南

面向在仓库内工作的 AI 编码代理（也适用于人类贡献者）。改代码前先通读本文件；
涉及架构改动时再读 `docs/agent_design/` 对应设计文档。

## 项目是什么

宵崎奏Bot：基于 NoneBot2 的 QQ 机器人（OneBot v11 + Console 双适配器），核心是
[Pydantic AI](https://pydantic.dev/docs/ai/overview/) 驱动的宵崎奏角色聊天 Agent
——流式对话、宿主工具 + MCP、mirage/sandlock 沙箱、主动回复、会话压缩与
SQLite 持久化、水晶计费；外围是娱乐与工具插件。

- Python **3.13+**，包管理一律用 **uv**（`uv run` / `uv pip` / `uv venv`），不用系统 `python3`
- 默认 PyPI 源为清华镜像（`pyproject.toml [tool.uv.index]`）
- 本机若有代理则外网受阻时使用；GitHub 依赖走 gh-proxy.org 镜像

## 常用命令

```bash
uv sync                                    # 安装依赖（可选组 rag：uv sync --with rag）
uv run playwright run-server --port 39211  # 渲染服务——必须先于 bot 启动
nb run                                     # 启动 bot
pytest tests/chat/                         # 单测门：只有 tests/chat/ 是维护范围
env PYTHONPATH=. uv run python tests/chat/<smoke>.py   # 冒烟脚本（须从仓库根运行）
ruff check kanade_bot/ && ruff format <改动文件>        # lint/format
```

- `uv run` 会自动重锁 `uv.lock`（版本漂移）——测试后 `git checkout -- uv.lock`
- 冒烟脚本自带 `asyncio.run` main（项目**无 pytest-asyncio**），不能直接 pytest 跑；
  `proactive_smoke_test` / `smoke_forward_tool_test` 已在 `tests/chat/conftest.py`
  的 collect_ignore 排除，其余 smoke 脚本的 pytest 收集错误为已知预存问题
- `tests/chat_legacy/`、部分 `tests/` 顶层脚本存在**预存**收集错误/失败，与你的改动
  无关时不必修复；判断标准：改动前先跑一遍留基线

## 仓库结构

```
bot.py                     # 入口：load_configs → init_nonebot →
                           #   register_adapters_and_load_plugins → patch_foreign_plugins
kanade_bot/plugins/        # 本地插件（pyproject [tool.nonebot] plugin_dirs 声明）
kanade_bot/utils/          # 跨插件工具：pai_runtime(模型)/billing(计费)/parse/persistence…
config/                    # YAML + 运行时配置；提示词在 config/chat/prompts/
config-{环境}.yaml          # 环境覆盖与密钥（gitignore）；anyconfig 深合并
schemas/                   # JSON Schema（config.yaml 顶部 $schema 引用，编辑器补全）
data/                      # 运行时数据；会话库 data/chat/sessions/{session_id}.sqlite3
cache/                     # localstore 缓存（gitignore）
docs/agent_design/         # ★ 权威设计文档（见下节）
scripts/                   # watchdog / session_viewer 等运维脚本
tests/chat/                # 真测试；tests/chat_legacy/ 为旧实验不维护
```

### 插件一览（kanade_bot/plugins/）

| 插件 | 职责 |
| --- | --- |
| **chat** | 核心：AI 聊天 Agent（占代码量大头，结构见下节） |
| crystal | 签到/水晶货币与消耗计费（chat 的 TTS/生成等工具都过余额预检） |
| model_updater | 配置热更新（web UI；会替换 chat_configs 实例指针） |
| tts | TTS 反向 WebSocket 网关（推理节点主动拨号，bot 是 server） |
| tool | 工具杂项：显卡日报/橘鸦AI早报/网安日报周报/MC状态/戳一戳/图片生成等 |
| summary / fun / gallery / help / imgtool / music / wordle / api60s / command_counter | 群总结 / 段子waifu决斗 / 画廊 / 帮助 / 图片处理 / 点歌 / 猜单词 / 60s新闻 / 命令统计 |

### chat 插件结构（速览）

```
plugins/chat/
├── matcher.py        # 全部 matcher 集中定义（用户约定：不按域拆）
├── handler/          # chat_flow(被动编排) / session_cmds / task_cmds / ban / proactive / meme_cmds
├── config/           # 按域拆分，__init__ 汇总导出（from ..config import cfg）
├── memes/            # 表情包语义检索（index/embedding/tagging）
├── deliver.py        # 分段投递（markdown 图片/表情包/代码块/合并转发）
└── agent/
    ├── manager.py / deps.py / prompt.py / vision.py / memory.py / schedule.py
    ├── session/      # runtime(ChatSession) / input_build / stats
    ├── history/      # store / gate_store / conv_db / blobs / compaction / budget
    ├── sandbox/      # hostenv / workspace / session / manager
    ├── tools/        # build_tools / media / messaging / memory / env / scheduling / common
    └── proactive/    # runtime / executor / gate / gate_history / windows / debounce
```

消息主链路：`matcher(chat) → handler/chat_flow → ChatSessionManager.get_session(sid)
→ ChatSession.send_and_wait`（会话锁覆盖整轮，含 finally 的缓冲清理与轮末压缩）→
`build_user_content`（分段 list[UserContent]）→ `agent.run_stream_events`
（MCP toolsets per-run 附加）→ 流式 `PartEndEvent` → `deliver` 分段投递 →
finally 轮末压缩 + 落库（`persist_run`）。

会话标识形如 `qq-group-{id}` / `qq-private-{id}` / `console-{group|private}-{id}`。

## 权威设计文档（改架构前必读）

| 文档 | 主题 |
| --- | --- |
| docs/agent_design/AGENT_DESIGN.md | chat agent 总体架构 |
| docs/agent_design/COMPACTION_DESIGN.md | 会话历史压缩（轮末压缩 + 在线应急阀 + 档位） |
| docs/agent_design/SESSION_STORE_DESIGN.md | 每会话一库存储 / marks / blobs / restore |
| docs/agent_design/SANDBOX_DESIGN.md | mirage + sandlock 沙箱 / venv 预配 / 生命周期 |
| docs/agent_design/PROACTIVE_DESIGN.md | 主动回复（防抖/概率闸/退避/决策历史） |
| docs/agent_design/MEME_DESIGN.md | 表情包语义检索 |
| docs/agent_design/MEMORY_DESIGN.md | 长期记忆 |

## 硬性约定（违反会破坏正确性或前缀缓存）

1. **在线压缩与恢复重放必须共用同一个 `apply_strategy`**；恢复历史的字节必须与
   关闭前最后一次请求一致（provider 前缀缓存命中是显式设计目标）
2. **系统提示词静态层字节必须稳定**：时间/每轮变化的内容不进 files 静态层
   （由 `prompt.current_time_line()` 附到用户消息）；提示词占位符是双花括号
   `{{var}}` 且只替换已知变量（`{{happy}}`、`{{表情包名称}}` 是发给模型的活约定，
   不能动）
3. **不缓存 `chat_configs.instance` 引用**（model_updater 会热替换实例指针），
   每次现读
4. `min_clear_tokens` 是**绝对 token 阈值**；只有 target 类压缩参数才走 fraction
5. pydantic 字段默认值必须落在最终子类字段上（父类默认值不参与校验会被绕过）
6. 沙箱会话**常驻**（create/close 幂等，`send_and_wait` finally 不关闭沙箱）；
   venv 解释器必须 symlink 形态 + 裸 `python3` 经 PATH 解析（--copies 真文件在
   沙箱内崩溃）
7. 子包内相对导入指向 chat 配置是**三级** `...config`（`..config` 会解析错）
8. 新增 `manager.py`/`runtime.py` 导入时，同步 `tests/chat/test_sandbox_fonts.py`
   的假包桩（kchat.agent.*），否则收集报 ImportError

## 测试套路

- **pytest 门 = `tests/chat/`**；冒烟脚本从仓库根直接运行（`env PYTHONPATH=.
  uv run python tests/chat/xxx.py` 或 `python -m tests.chat.xxx`）
- 需要完整插件链的测试复用 bot.py 四步：`load_configs` → `init_nonebot` →
  `register_adapters_and_load_plugins` → `patch_foreign_plugins`；**不能**直接
  `import kanade_bot.plugins.chat...`（nonebot 自定义 MetaPathFinder 会抛 KeyError），
  要用 `sys.modules["kanade_bot.plugins.chat.agent.manager"]` 取
- 绕开插件链的单测用假包链（`kchat.agent.__path__=[真实目录]` + sys.modules 桩），
  先例见 `tests/chat/test_mirage_backend.py` / `test_pai_agent.py`
- driver 用 `~fastapi`（`~none` 没有 app，localstore 会 assert 失败）
- `FunctionModel` 测试模型会合成 usage 锚点：断言 output 只能 >0；触发在线压缩
  须显式压低 online_valve_fraction
- 涉及渲染的测试需 `render={"provider":"playwright","startup":"off",
  "provider_config":{"skip_browser_install":True,...}}`，否则会尝试 sudo 装系统依赖

## 配置与 Schema

- `config.yaml`（基础）+ `config-{环境}.yaml`（覆盖/密钥，gitignore）经 anyconfig
  深合并；仍支持 .env DotEnv 方式；`localstore_use_cwd: true` → cache/、data/ 落
  仓库根相对路径
- 插件运行时配置（chat_configs.json / crystal_config.json 等）在 `config/` 各子目录
- 新增配置字段后重新生成 schema 获得编辑器补全：
  `GENERATE_SCHEMAS=true uv run python -c "import bot; from pathlib import Path;
  env,cfg=bot.load_configs(Path(bot.__file__).parent); bot.init_nonebot(env=env,**cfg);
  bot.register_adapters_and_load_plugins();
  bot.register_other_configs_and_generate_schema()"`（勿调 nonebot.run()；生成完
  改回 false）

## 代码风格

- ruff：`line-length = 100`；lint 只 ignore `B008 / RUF012 / BLE001`，其余默认集；
  全仓 `ruff check kanade_bot/` 零错误是基线，新代码随手 `ruff format` 改动文件
- Python 3.13+ 语法可用；依赖变更走 `uv add/remove`（注意 `pydantic-ai-slim[mcp]`
  **不含** openai extra，删除时别连带删掉）

## 运维/部署事实

- 图片渲染：htmlrender 0.8+ 连接**独立** playwright server（同 venv、先启动）；
  bot 退出只断连不关浏览器
- GitHub watchdog（`scripts.github_watchdog.py`）轮询提交自动 `git pull --ff-only`
  并重启——生产为独立副本，仓库内配置/文档改动需同步
- 沙箱依赖 sandlock（要求 Landlock ABI v6 / Linux 6.12+；低版本自动降级仅削弱
  防进程逃逸，文件系统隔离仍可用）；mirage-ai 现行 0.1.0
- 沙箱默认关闭（`sandbox.enabled: true` 开启）

## 改动自检清单

1. `ruff check kanade_bot/`（零新增）
2. `pytest tests/chat/`（对比改动前基线）
3. 涉及 chat 流程/投递/压缩：跑对应冒烟脚本
4. 涉及配置字段：重新生成 schema + 同步 config-example.yaml
5. `grep` 被重命名/移除的旧符号确认无残留 import
6. 提交前 `git checkout -- uv.lock`（若只是跑过测试）
