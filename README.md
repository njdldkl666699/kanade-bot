<div align="center">
  <img src="https://gh-proxy.org/https://raw.githubusercontent.com/njdldkl666699/kanade-bot/refs/heads/main/Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em;"/>
  <h1>宵崎奏Bot (Kanade Bot)</h1>
  <p><picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://pydantic.dev/docs/ai/img/pydantic-ai-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="https://pydantic.dev/docs/ai/img/pydantic-ai-light.svg">
    <img style="width: 15em;" alt="Pydantic AI" src="https://pydantic.dev/docs/ai/img/pydantic-ai-dark.svg">
  </picture></p>
  <a href="./LICENSE"><img src="https://img.shields.io/github/license/njdldkl666699/kanade-bot.svg" alt="license"></a>
  <img src="https://img.shields.io/badge/python-3.13+-blue.svg" alt="python">
  <img alt="GitHub last commit" src="https://img.shields.io/github/last-commit/njdldkl666699/kanade-bot">
</div>

## 简介

宵崎奏Bot是一个基于[NoneBot2](https://nonebot.dev/)框架的机器人，使用[Pydantic AI](https://pydantic.dev/docs/ai/overview/)开发聊天Agent，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

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

启用前需安装 **sandlock** CLI，并添加到`PATH`环境变量中。

```bash
# 从 https://github.com/multikernel/sandlock/releases 下载对应架构的二进制
curl -fsSL -o sandlock.tar.gz \
  https://github.com/multikernel/sandlock/releases/download/v0.8.9/sandlock-x86_64-unknown-linux-gnu.tar.gz
tar xzf sandlock.tar.gz && install -m755 sandlock ~/.local/bin/
sandlock check   # 确认 ABI 与 protection 可用性
```

**内核版本要求**：sandlock 完整规则集要求 Landlock ABI v6（Linux 6.12+）。在低于此版本的内核上，bot会生成wrapper并降级到 Landlock ABI v4。若连v4也无法满足，则沙箱无法启动。降级只削弱防进程逃逸，文件系统隔离仍可用。

### 配置

`config.yaml` 顶部已声明 `$schema: ./schemas/MergedConfig.json`，在支持 YAML Schema 的编辑器（如 VS Code + YAML 插件）中可自动补全全部配置项。

`config-{环境}.yaml`被`.gitignore`忽略，可以存放敏感信息（如API Key、Token等），也可用作不同环境的配置覆盖。

同时，也支持NoneBot原本的DotEnv环境变量配置方式，仍可使用`.env` `.env.{环境}`文件来配置。

优先级请参考NoneBot官方文档。yaml通过直接传入的方式，优先级最高，yaml环境配置高于yaml基础配置。

此外，本项目配置加载使用`anyconfig`，支持多种格式（YAML、JSON、TOML、INI等），可自行扩展。

## Watchdog（自动更新与重启）

Watchdog 用于轮询 GitHub 最新提交，当检测到更新时自动 `git pull --ff-only` 并重启核心进程（`nb run`）。

使用方式：

1. 配置Watchdog只需在配置文件中修改`watchdog`段即可。
2. 启动 Watchdog：`uv run -m scripts.watchdog`

## 生成JSON Schema

本Bot使用了很多配置类来管理插件的配置项，它们通过读取`config/`目录下的一些JSON文件来加载配置。

为了在编写时获得更好的类型提示和自动补全，可以设置`generate_schemas: true`，然后运行Bot，这次运行就会在`schemas/`目录下生成对应的JSON Schema文件，包括合并了全部插件配置的`MergedConfig.json`。生成完毕后建议改回`false`。

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