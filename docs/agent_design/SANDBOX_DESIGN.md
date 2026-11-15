# 沙箱设计（Sandbox）

> 实现位置：`kanade_bot/plugins/chat/agent/sandbox/`
> 为聊天 Agent 提供文件与 shell 能力，并把执行边界从宿主进程隔离开。

## 1. 目标与选型

Agent 需要读写文件、执行命令（渲染 HTML、跑 Python 脚本、分析用户发的图），这些能力不能直接跑在 bot 进程里——bot 持有 API Key 与全部会话数据。方案演进：Docker（容器冷启动 + 每容器数十 MB）→ 现行 **mirage + sandlock**：

| 组件 | 角色 | 关键事实 |
| --- | --- | --- |
| [mirage](https://github.com/strukto-ai/mirage)（PyPI 0.0.7） | 进程内虚拟文件系统（VFS）+ 会话 shell | 每活跃 workspace 约 0.1MB RSS；官方提供 `MirageWorkspaceBackend`（Pydantic AI workspace 协议实现） |
| [sandlock](https://github.com/multikernel/sandlock) CLI | 受限子进程的 OS 级边界 | Landlock（文件系统）+ seccomp（系统调用/网络）；每次执行 spawn 新 sandlock 进程 |
| Pydantic AI `workspaces` | 工具协议 | ConsoleCapability 提供 shell/read/write/edit/grep/glob 等客户端工具，经 `get_workspace` 取后端 |

不用 bubblewrap（实测沙箱内可读到 bot 的 `config.yaml` 含密钥）。mirage 依赖演进：早期 PyPI 0.0.7a2 相对 git main 有 3 个缺陷（cwd 不传、相对路径读写落根、sandlock 子进程 cwd 错误）而临时改用 git main；0.0.7 正式版已包含全部修复（2026-10-21 实测确认），依赖已切回 PyPI。

## 2. 隔离模型

双层边界：

1. **VFS 视图层**（mirage）：每个会话一个 `Workspace`，唯一挂载是自己的工作区目录。界外路径在 VFS 里不存在，工具的相对路径天然无法逃逸。
2. **进程层**（sandlock）：经 captures 路由到 `SandlockRuntime` 的命令（`python3`/`python` 及动态注册的 venv entry point）由 sandlock 拉起受限子进程，受 Landlock 文件系统规则（读写白名单）、seccomp（出站网络默认全禁）、内存上限（512M）约束。**shell allowlist 不是隔离**，边界在执行环境。

例外是 **mirage 内置命令**（`curl`/`gh`/`cat`/`head` 等 mirage 自带实现的命令）：它们在宿主 bot 进程内执行，不经 sandlock——文件访问仍经 VFS 判定（界外不可见），但**网络访问走宿主**（实测内置 `curl` 可直接取回 URL 内容；沙箱内受限进程联网仍被 seccomp 拒绝）。bot 自身本就持有宿主网络，这不构成新的密钥泄露面，但评估模型行为与审计网络请求时应知道 `curl` 不受沙箱网络策略约束。未注册的外部命令（如 `ffmpeg`）返回 127。

**免 FUSE 布局**是两者的粘合点：VFS 挂载前缀 = 宿主真实路径（虚拟路径与真实路径一致），sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。该布局下官方 backend 收到相对路径会落到 VFS 根 `/` 的合成视图（0.0.7 实测：overlay 内可读回，但**宿主磁盘无文件、沙箱进程不可见**）——`KanadeWorkspace` 子类把全部文件操作（read/write/stat/list_dir/make_dir/exists/remove/realpath）的相对路径绑定到工作区根，与 shell 会话 cwd 对齐。

## 3. 对象模型

```
SandboxManager（进程内单例，随 chat 启用）
 ├─ _sessions: {session_id → SandboxSession}     # 会话常驻注册表
 ├─ _check_uv / _resolve_host_python             # 启动期宿主侧探测
 └─ create(session_id) → SandboxSession           # get-or-create
      ├─ workspace: mirage Workspace              # DiskVFS 挂载 + SandlockRuntime
      ├─ backend: KanadeWorkspace                 # Pydantic AI workspace 协议
      ├─ setup_venv(...)                          # 宿主侧预配 Python 环境
      └─ close()                                  # 幂等；文件保留仅丢 shell 状态

SandboxWorkspaceCapability                         # Agent capability
 └─ get_workspace(ctx) → deps.sandbox.backend      # 每会话分发，官方单 workspace 方案不适用
```

命令执行语义（官方 `MirageWorkspaceBackend.run`）：每次在会话克隆上执行，`cd`/`export` 不跨命令持久；但会话级 `workspace.shell(..., session_id=)` 的 cwd/env 状态持久，run 的克隆**继承**会话 env——初始化的 `cd 工作区根` 与 venv PATH 注入都靠这条落在会话状态上。

生命周期为**会话常驻**（演进：TTL/LRU 池 → 每轮建关 → 常驻；池化实测仅省约 5ms/轮的 Python 层构造，常驻是为保留 shell cwd/export 跨轮状态）。关闭时机仅两个：会话重置（close + 删工作区目录）与进程 shutdown；轮次的 finally 不关闭。工作区文件在宿主磁盘持久，关闭只丢 shell 会话状态。

## 4. 授权与环境（sandbox_runtime_config）

sandlock 受限子进程的统一授权：

| 项 | 值 | 理由 |
| --- | --- | --- |
| `fs_readable` | 工作区 + uv 托管解释器目录 + 字体目录 | venv 的 `bin/python` 是指向 uv 解释器的 symlink，须授权整个解释器目录 |
| `fs_writable` | 仅工作区 | |
| `max_memory` | 512M（配置） | |
| `PATH` | `{工作区}/.venv/bin` | **不含** /usr/bin 与 /bin——不提供系统 python3，`python3` 只能解析到 venv 解释器（Ubuntu merged-usr 下排除系统 python3 须同时去掉两者） |
| `HOME`/`XDG_*` | `{工作区}/.home` | `--clean-env` 后需要可写 HOME，否则 fontconfig 缓存无落点 |

用户图片在发送前写入工作区 `images/`（同图重发复用，冲突加 uuid 前缀），提示词附相对路径说明，工具即可直接访问。

## 5. Landlock ABI 降级

sandlock 完整规则集要求 Landlock ABI v6（Linux 6.12+）；低于 v6 但 ≥ v4 时缺三项 protection（SignalScope / AbstractUnixSocketScope / FsIoctlDev）。降级机制：

- `landlock_degrade`：`auto`（默认，按 `sandlock check` 判断）/ `always` / `strict`（不足即拒绝启动）。ABI < v4 连文件系统规则都保不住，任何模式都报错。
- 实现 = **PATH shim wrapper**：在 PATH 前置目录生成 `sandlock` 脚本，在 `run` 子命令之后、`--` 之前插入三条 `--allow-degraded`，再 exec 真二进制（mirage 的 `SandlockRuntime` 硬编码 argv 无透传接口；wrapper 用 `sub="$1"; shift; exec ... "$@"`，`${@:2}` 是 bash 扩展 dash 不支持，会静默失效）。
- **自检防静默失效**：生成后立即用 wrapper 跑探针命令。探针授权 = 解释器根 + 系统只读目录（/usr /lib /lib64 /bin /etc /proc /dev）——只授权解释器根不够：解释器是动态链接 ELF，execve 还要读 ld-linux/libc，CPython 启动还要读 /dev/urandom，任一被拒都直接 EACCES，极易误判为执行权限问题。uv run 会把 .venv/bin 前置 PATH 导致 `which("python3")` 解析到 /home 下的软链，系统根必须显式补齐（交互 shell 直接 `nb run` 时探针根恰为 /usr，才没暴露此问题）。
- 安全影响：降级**不破坏文件系统隔离主线**（Landlock FS rules + mirage VFS 不受影响，内存限额保留），只削弱防进程逃逸维度（沙箱内进程可 signal 宿主同用户进程、连 abstract unix socket、对设备 ioctl）。

## 6. Python 环境（宿主预配 venv）

**装包必须在宿主侧**：沙箱内出站网络默认禁（mirage 的 sandlock runtime 不透传网络授权），`uv pip install` 在沙箱内下载不了包；`--clean-env` 后 uv 默认目录也无授权。因此 venv 一律在**宿主侧**创建（走宿主网络与镜像），沙箱内只 exec：

`setup_python_env` 工具（模型可调用）的三级回退：

1. **uv**：`uv venv --python {venv_python}` + `uv pip install --python {venv}/bin/python`（默认启用）
2. **原生 venv**：宿主 `python -m venv` + venv 内 pip（无 uv 时；Debian 拆包缺 ensurepip 则降级 `--without-pip` 裸环境，标准库可用不能装包）
3. 都不可用：报告模型不可用（启动期已探测并 warning）

约束与细节：

- **保持默认 symlink 形态**：venv 解释器以 symlink 引用宿主解释器，不复制文件。历史上 sandlock <0.9.0 的 readlink bug 曾使 `--copies` 真文件解释器在沙箱内启动崩溃（CPython getpath 的探测），0.9.0 已修复；symlink 仍是默认且更省空间的形态，无理由切换。
- **包声明白名单**：`name` / `name[extras]` / `name==version` 纯文本正则，拒绝 URL/git/本地路径——装包命令在宿主侧执行，必须防注入。`--no-build` / `--only-binary` 限制已知情地禁用（需要纯 sdist 包，接受宿主构建风险）。
- **entry point 注册**：装包后 venv/bin 新增命令（如 pytest）经 `workspace.add_runtime()` 动态挂进沙箱路由（runtime 名 `venv-bin-{n}` 唯一递增），同时会话 `export PATH` 更新；`python3 -m 模块名` 则无需注册。重启后 venv 直接可用，entry point 命令需再次调用工具补注册。
- **装包后的会话 PATH**：更新为 `{venv}/bin:/usr/bin:/bin`——系统工具目录此时才引入（venv 已就位，`python3` 必然解析到 venv 解释器），与 §4 初始 PATH 只含 venv bin 的策略不矛盾。
- **回退解释器解析**（无 uv 时）：优先 `python{venv_python}`（常为带完整 ensurepip/pip 的 uv 托管或自装解释器），否则系统 `python3`；选中解释器的安装根在系统目录之外（沙箱未授权）时补进只读授权，保证 venv 的解释器 symlink 在沙箱内可执行。
- Python 环境不可用时，提示词的 Python 环境段（`sandbox_python.md`）整段不渲染（条件段 `python_env_available`）。

## 7. 配置（SandboxConfig）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | false | 总开关；关闭时无文件/shell 工具 |
| `environment` | {} | 注入每会话的 env（对工作区进程可见，勿放密钥） |
| `memory_limit` | 512M | sandlock 子进程内存上限 |
| `workspace_dir` | sandboxes/ | 工作区根（插件数据目录下，每会话一个子目录） |
| `landlock_degrade` | auto | ABI 不足策略：auto / always / strict |
| `landlock_real_binary` | None | 真 sandlock 路径；默认 `which` 动态求值 |
| `uv_bin` | uv | uv 可执行文件名或绝对路径 |
| `uv_python_dir` | None | uv 托管解释器目录；默认 `uv python dir` 求值 |
| `venv_python` | 3.13 | venv 默认 Python 版本 |
| `venv_timeout` | 300 | 宿主侧命令超时秒数 |

## 8. 已知限制与边界事实（mirage 0.0.7 实测）

- mirage 内置命令在宿主进程内执行（见 §2），`curl` 的网络访问不受沙箱约束。
- `MountMode` 必须 EXEC（WRITE 下 python3 → exit 126）。
- 工作区目录必须 `realpath()`，否则写进 overlay 而宿主目录为空。
- 官方 backend 裸相对路径仍落 VFS 根 overlay（VFS 内读得到、宿主无文件），而 `remove`/`realpath` 按会话 cwd 解析——`KanadeWorkspace` 统一绑定为绝对路径规避该不一致。
- AI 敲绝对路径命令会被 basename 化经会话 PATH 重解析（防绕过，属设计而非缺陷）。
- 受限进程的会话 cwd 不在宿主磁盘上时拒绝执行（exit 126）。

0.0.7 已修复（旧文档记录、现已不适用）：内置 `curl` 的 `-m`/`-k` 误当端口（exit 7）；`/dev` 只读授权下 `>/dev/null` 报 Permission denied（null sink 现可写）；backend 写文件需手动建父目录（现自动创建）；`remove` 可删除会话工作目录（现拒绝 cwd 及其祖先）。
