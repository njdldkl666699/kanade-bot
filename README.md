# 宵崎奏Bot（Kanade Bot）

<img src="assets/宵崎奏Ciallo.webp" alt="Ciallo～(∠・ω< )⌒☆" style="width: 20em"/>

## 简介

宵崎奏Bot是一个基于NoneBot2框架的机器人，集成了Copilot SDK来提供聊天功能，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

## 部署

1. 克隆仓库到本地；
2. 安装依赖：`uv sync`；
3. 参考各个插件的配置类，补全或修改`.env`和`.env.prod`中的配置项；
4. 运行机器人：`uv run`。

## 启用RAG

RAG功能默认是启用的，因此你需要在`4. 运行机器人`之前，先执行以下步骤来初始化RAG数据库：

1. 启动ChromaDB服务端：`chroma run [--path <PATH>] [--port <PORT>]`，`<PATH>`是你存储RAG数据库的路径，默认为当前目录下的`chroma`文件夹；`<PORT>`是监听的端口，默认为8000，必须与`.env`或`.env.prod`中的`CHAT_RAG_PORT`配置项一致。
2. 初始化向量数据库：`uv run scripts/init_kanade_wiki_rag_db.py`。

如果你不需要RAG功能，可以在env中设置`CHAT_RAG_ENABLED=false`来禁用它。