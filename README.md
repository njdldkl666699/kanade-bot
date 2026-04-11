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

1. `Fontconfig error: Cannot load default config file: No such file: (null)`

    Windows上没有FontConfig，可以在env中配置`FONTCONFIG_PATH="assets/"`来指定字体配置目录，其下的`fonts.conf`文件会被加载为配置文件。

    大部分Linux发行版是有FontConfig的，出现这个问题也可以在env中配置`FONTCONFIG_PATH`来指定字体配置目录，一般为`/etc/fonts/`。