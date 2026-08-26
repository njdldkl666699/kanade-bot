from magika import Magika

m = Magika()
res = m.identify_bytes(
    """## 简介

宵崎奏Bot是一个基于NoneBot2框架的机器人，集成了Copilot SDK来提供聊天功能，并提供一些有趣的功能命令。同时支持Console和OneBot v11适配器，方便在不同环境中使用。

## 部署

1. 克隆仓库到本地；
2. 安装依赖：`uv sync`；
3. 创建`config-{环境}.yaml`配置文件，补全`config.yaml`和`config-{环境}.yaml`中的配置项；
4. 运行机器人：`nb run`。""".encode()
)
print(res.output.label)  # 输出: 'markdown'[reference:4]
