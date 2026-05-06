# 宵崎奏Bot（Kanade Bot）

<img src="assets/images/宵崎奏Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em"/>

![Stone Badge](https://stone.professorlee.work/api/stone/njdldkl666699/kanade-bot)

## 简介

宵崎奏Bot是一个基于NoneBot2框架的机器人，集成了Copilot SDK来提供聊天功能，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

## 部署

1. 克隆仓库到本地；
2. 安装依赖：`uv sync`；
3. 参考各个插件的配置类，补全或修改`.env`和`.env.prod`中的配置项；
4. 配置RAG功能（可选）：在`.env`或`.env.prod`中设置`CHAT_RAG_ENABLED=true`（默认为false），配置`scripts/kanade_rag_server.py`中所需env，并使用`uv run scripts/kanade_rag_server.py`来启动RAG RPC服务端；
5. 运行机器人：`nb run`。

## Watchdog（自动更新与重启）

Watchdog 用于轮询 GitHub 最新提交，当检测到更新时自动 `git pull --ff-only` 并重启核心进程（`nb run`）。

使用方式：

1. 在`.env`或`.env.prod`中设置：
   - `WATCHDOG_ENABLED=true`
   - `WATCHDOG_GITHUB_REPO=owner/repo`
   - `WATCHDOG_GITHUB_BRANCH=main`
   - `WATCHDOG_GITHUB_TOKEN=...`（可选，用于提高 API 限额）
   - `WATCHDOG_POLL_INTERVAL=30`
2. 启动 Watchdog：`uv run scripts/kanade_watchdog.py`

## 常见问题

1. 服务器查询返回的图片字体不好看：支持Unifont，可以下载Unifont字体并安装到系统中。
2. 终端打印的Banner错乱：
   1. 检查你的终端模拟器是否支持True Color（24-bit颜色）。如果不支持，可能会导致颜色显示异常。
   2. 如果在Windows Terminal中显示不正确，请检查对应配置文件-外观-自动调整无法区分的文本的亮度的设置；如果为“始终”，改为其他选项即可正常显示。
3. 关闭聊天功能并使用AstrBot：
   1. 在`.env`或`.env.prod`中设置`CHAT_ENABLED=false`来关闭聊天插件的所有功能。
   2. 本项目提供了一个本地Embedding服务器，你可以使用它来为AstrBot提供嵌入模型。使用`uv run scripts/openai_embedding_server.py`来启动，并参考其中的配置在AstrBot中填入。