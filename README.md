<div align="center">
  <img src="https://gh-proxy.org/https://raw.githubusercontent.com/njdldkl666699/kanade-bot/refs/heads/main/Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em;"/>
  <h1>宵崎奏Bot (Kanade Bot)</h1>
  <a href="./LICENSE">
    <img src="https://img.shields.io/github/license/njdldkl666699/kanade-bot.svg" alt="license">
  </a>
  <img src="https://img.shields.io/badge/python-3.13+-blue.svg" alt="python">
  <img alt="GitHub last commit" src="https://img.shields.io/github/last-commit/njdldkl666699/kanade-bot">
</div>

## 简介

宵崎奏Bot是一个基于NoneBot2框架的机器人，集成了Copilot SDK来提供聊天功能，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

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

### 程序长久不启动，或运行到`COPILOT_CLIENT = CopilotClient()`时报错

1. 检查网络连接是否正常，确保可以访问GitHub Releases；
2. 参考如下环境变量配置，在启动程序时或为终端设置环境变量：
   ```bash
   # Copilot SDK 的 CLI 路径，如果为空，则会下载CLI
   COPILOT_CLI_PATH=
   # Override the cache directory (binary placed directly here).
   COPILOT_CLI_EXTRACT_DIR=
   # Set to "1" or "true" to disable auto-download.
   COPILOT_SKIP_CLI_DOWNLOAD=
   # Override the GitHub Releases base URL.
   COPILOT_CLI_DOWNLOAD_BASE_URL="https://gh-proxy.com/https://github.com/github/copilot-cli/releases/download"
   ```
