# Python 环境

- 沙箱内没有系统 python3。要运行 Python，先用 `setup_python_env` 工具创建虚拟环境（可同时传入要安装的包），之后 `python3` 才可用。
- 沙箱内无法自行联网安装包；需要任何第三方包都必须通过 `setup_python_env` 安装，之后可直接 `import`。
- 安装自带命令行工具的包后，其命令可直接调用（如 `pytest --version`），也可用 `python3 -m 模块名` 调用。
- 虚拟环境位于工作区 `.venv/`，与工作区文件一样在会话间保留。
