<div align="center">
  <img src="https://gh-proxy.org/https://raw.githubusercontent.com/njdldkl666699/kanade-bot/refs/heads/main/Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em;"/>
  <h1>宵崎奏Bot (Kanade Bot)</h1>
  <a href="./LICENSE"><img src="https://img.shields.io/github/license/njdldkl666699/kanade-bot.svg" alt="license"></a>
  <img src="https://img.shields.io/badge/python-3.13+-blue.svg" alt="python">
  <img alt="GitHub last commit" src="https://img.shields.io/github/last-commit/njdldkl666699/kanade-bot">
</div>

## 简介

宵崎奏Bot是一个基于[NoneBot2](https://nonebot.dev/)框架的机器人，使用[OpenAI Agents SDK](https://openai.github.io/openai-agents-python/zh/)开发聊天Agent，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

## 部署

1. 克隆仓库到本地；
2. 安装依赖：`uv sync`；
3. 创建`config-{环境}.yaml`配置文件，补全`config.yaml`和`config-{环境}.yaml`中的配置项；
4. 运行机器人：`nb run`。

### 依赖

本项目使用可选依赖组`rag`来支持RAG功能，如果需要使用RAG功能，请安装依赖：

```bash
uv sync --with rag
```

### 沙箱（可选）

聊天Agent的文件与shell能力由 [mirage](https://github.com/strukto-ai/mirage) 沙箱提供。启用方式：

```yaml
# config-{环境}.yaml
sandbox:
  enabled: true
```

启用前需安装 **sandlock** CLI：

```bash
# 从 https://github.com/multikernel/sandlock/releases 下载对应架构的二进制
curl -fsSL -o sandlock.tar.gz \
  https://github.com/multikernel/sandlock/releases/download/v0.8.9/sandlock-x86_64-unknown-linux-gnu.tar.gz
tar xzf sandlock.tar.gz && install -m755 sandlock ~/.local/bin/
sandlock check   # 确认 ABI 与 protection 可用性
```

`~/.profile` 里的 `PATH` 需包含 sandlock 所在目录（登录 shell 会自动加载）。
sandlock 缺失时机器人会在启动阶段直接报错并提示，不会静默降级；临时不用沙箱可把
`sandbox.enabled` 设为 `false`。

**内核版本要求**：sandlock 完整规则集要求 Landlock ABI v6（Linux 6.12+）。
`Landlock ABI v4`（如 Linux 6.8）上需对 v6 protection 显式降级，否则 sandlock
默认 Strict 模式会拒绝启动。**机器人会自动处理**：启动时读 `sandlock check` 的
ABI，低于 v6 时在工作区根的 `.bin/` 生成一个 wrapper 注入
`--allow-degraded signal-scope --allow-degraded abstract-unix-socket-scope
--allow-degraded fs-ioctl-dev`，并把该目录前置到 `PATH`（mirage 用
`shutil.which("sandlock")` 查找，会命中 wrapper）。wrapper 生成后会自检一次，
降级没生效则拒绝启动。

降级只削弱「防进程逃逸」维度（signal 宿主进程 / 连宿主 abstract unix socket /
设备 ioctl），**文件系统隔离主线不受影响**——工作区外的路径在 mirage VFS 中依然
不存在，且 `max_memory` 仍由 rlimit 生效。可用 `sandbox.landlock_degrade` 改为
`strict`（不足即报错）或 `always`（始终降级）。

沙箱工作区位于插件缓存目录的 `sandboxes/<会话ID>/`，每个会话独立，文件在会话间
保留。
Python 由宿主 CPython 执行并受 Landlock 约束，只能访问自己的工作区。

已知限制：内置 `curl` 的 `-m`（超时）与 `-k`（忽略证书）参数暂不支持，会返回
退出码 7；请使用 `-s` / `-L` / `-o <文件>`。

### 会话历史与压缩

会话历史全量落 SQLite（`messages` 表，只追加不修改），压缩事件另记在
`compaction_marks` 表；重启时按 marks 重放，重建的历史与关闭前最后一次请求发送
的内容完全一致，以最大化 provider 侧的前缀缓存命中率。

压缩只清空旧工具**结果**的内容（消息结构与位置不变，前缀稳定），**不用滑动
窗口**——滑动窗口每轮移动前缀起点会让前缀缓存全部失效。相关配置：

```yaml
chat:
  compaction:
    trigger_fraction: 0.8      # 上下文占用超过此比例才触发压缩
    keep_pairs: 3              # 保留最近几个工具调用对
    min_clear_tokens: 2000    # 清理收益太小就跳过，保护缓存
    summary_target_fraction: null # 设了才启用 LLM 摘要档（默认不启用）
    summary_model: null        # 摘要模型，null = 继承主模型
    summary_keep_messages: 40  # 生成摘要时保留的最近消息条数
```

比例均相对**当前模型的上下文窗口**解析，换模型不必重新校准。
`summary_target_fraction` 留空时**只用零成本档**，压缩永远可重放、不调用 LLM。
启用摘要档后摘要结果会一并存入 mark，恢复时直接复用而不重新生成。

会话历史存储（消息缓冲区 + 数据库）与持久化记忆各自独立成子配置：

```yaml
chat:
  session:
    db_file: agent_sessions.sqlite3      # 数据库（插件数据目录）
    buffer_max_size: 100                 # 消息缓冲区条数
    buffer_cache_file: session_messages_cache.json # 缓冲区缓存（插件缓存目录）
  memory:
    database_file: memories.sqlite3      # 记忆数据库（插件数据目录）
    max_records_per_scope: 256           # 每个用户/群聊的记忆条数上限
```

`/清理会话存储` 现在只报告统计（全量条数 / 实际发送条数），**不再删除消息**——
数据库是全量保留的。

### 配置

`config.yaml` 顶部已声明 `$schema: ./schemas/MergedConfig.json`，在支持 YAML Schema 的编辑器（如 VS Code + YAML 插件）中可自动补全全部配置项。

`config-{环境}.yaml`被`.gitignore`忽略，适合存放敏感信息（如API Key、Token等），也可用于不同环境的配置覆盖。

同时，也支持NoneBot原本的DotEnv环境变量配置方式，仍可使用`.env` `.env.{环境}`文件来配置。

优先级请参考NoneBot官方文档，yaml通过直接传入的方式，优先级最高，yaml环境配置高于yaml基础配置。

此外，本项目配置加载使用`anyconfig`，支持多种格式（YAML、JSON、TOML、INI等），可自行扩展。

## Watchdog（自动更新与重启）

Watchdog 用于轮询 GitHub 最新提交，当检测到更新时自动 `git pull --ff-only` 并重启核心进程（`nb run`）。仅支持POSIX规范的系统（如Linux、MacOS），不支持Windows。

使用方式：

1. 配置 Watchdog（二选一，YAML 优先级更高）：
   - YAML：在`config.yaml`或`config-{环境}.yaml`中设置`watchdog:`段：
     ```yaml
     watchdog:
       github_repo: owner/repo
       github_branch: main
       github_token: "..." # 可选，用于提高 API 限额
       poll_interval: 30
     ```
   - 环境变量：在`.env`中设置 `WATCHDOG__GITHUB_REPO=owner/repo` 等；
2. 启动 Watchdog：`uv run -m scripts.watchdog`

## 生成JSON Schema

本Bot使用了很多配置类来管理插件的配置项，它们通过读取`config/`目录下的一些JSON文件来加载配置。为了在编写时获得更好的类型提示和自动补全，可以设置`config.yaml`或`config-{环境}.yaml`中的配置项`generate_schemas: true`（或`.env`中的`GENERATE_SCHEMAS=true`），然后运行Bot，这次运行就会在`schemas/`目录下生成对应的JSON Schema文件（包括合并了全部配置的`MergedConfig.json`）。生成完毕后建议改回`false`。

## 常见问题

### MC服务器状态查询返回的字体问题

仓库中编写的模板文件添加了Unifont字体，如果字体不美观，可以下载它并安装到系统中。

### 终端打印的Banner错乱

1. 检查你的终端模拟器是否支持True Color（24-bit颜色）。如果不支持，可能会导致颜色显示异常。
2. 如果在Windows Terminal中显示不正确，请检查对应配置文件-外观-自动调整无法区分的文本的亮度的设置；如果为“始终”，改为其他选项即可正常显示。

### Failed to build `lxml==x.x.x`

请根据你的操作系统，执行以下命令安装必要的系统依赖：

**Ubuntu / Debian**

```bash
sudo apt update
sudo apt install libxml2-dev libxslt1-dev python3-dev
```

**CentOS / RHEL / Fedora**

```bash
# CentOS 7/8
sudo yum install libxml2-devel libxslt-devel python3-devel

# Fedora / RHEL 8+
sudo dnf install libxml2-devel libxslt-devel python3-devel
```

**macOS**

```bash
# 1. 安装 Xcode 命令行工具（如果尚未安装）
xcode-select --install

# 2. 使用 Homebrew 安装依赖库
brew install libxml2 libxslt
```

安装完成后，如果 `uv` 仍然找不到库，可以尝试在安装时显式指定路径（M1/M2 芯片路径通常是 `/opt/homebrew/`）：

```bash
export LDFLAGS="-L$(brew --prefix libxml2)/lib -L$(brew --prefix libxslt)/lib"
export CPPFLAGS="-I$(brew --prefix libxml2)/include/libxml2 -I$(brew --prefix libxslt)/include"
uv add PicImageSearch
```