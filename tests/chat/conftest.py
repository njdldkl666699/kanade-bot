"""tests/chat 收集配置

两个冒烟脚本（`autoreply_smoke_test.py` / `smoke_forward_tool_test.py`）设计为
直接运行（`uv run python tests/chat/xxx.py`，顶层执行生产版 NoneBot 初始化），
与 pytest 同进程内的其他测试会双重初始化插件冲突，故排除出收集范围。
"""

collect_ignore = ["autoreply_smoke_test.py", "smoke_forward_tool_test.py"]
