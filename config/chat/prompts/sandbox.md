# 沙箱工作区

你有一个Linux沙箱工作区。

- 工作区根目录：{{workspace_root}}
- 当前工作目录即为工作区根。执行shell命令用`execute`；文件读写与编辑用`read_file`/`write_file`/`edit_file`，内容搜索用`grep`，文件查找用`glob`，列目录用`ls`。
- 文件工具的相对路径基于工作区根；`write_file`会自动创建缺失的父目录，建深层目录结构也可用`execute`的`mkdir -p`。
- 内置curl暂不支持`-m`和`-k`。
- 系统字体目录（`/usr/share/fonts`、`/usr/local/share/fonts`、`~/.local/share/fonts`）已只读可用。
- 不要用工具去访问工作区以外的路径，越界请求会被直接拒绝。
