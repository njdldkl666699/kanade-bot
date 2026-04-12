# 宵崎奏Bot（Kanade Bot）

<img src="assets/宵崎奏Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em"/>

## 简介

宵崎奏Bot是一个基于NoneBot2框架的机器人，集成了Copilot SDK来提供聊天功能，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

## 部署

1. 克隆仓库到本地；
2. 安装依赖：`uv sync`；
3. 参考各个插件的配置类，补全或修改`.env`和`.env.prod`中的配置项；
4. 配置RAG功能（可选）：在`.env`或`.env.prod`中设置`CHAT_RAG_ENABLED=true`（默认为false），配置`scripts/kanade_rag_server.py`中所需env，并使用`uv run scripts/kanade_rag_server.py`来启动RAG RPC服务端；
5. 运行机器人：`nb run`。

## 常见问题

1. 图片没有字体：阅读`assets/fonts.conf`，自行下载这些字体；也可以修改为其他字体。