import asyncio
import os
import posixpath
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from mirage import MountMode, Workspace
from mirage.agents.pydantic_ai import MirageWorkspaceBackend
from mirage.runtime.sandbox.sandlock import SandlockConfig, SandlockRuntime
from mirage.vfs.disk import DiskVFS
from nonebot import get_driver, logger
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.workspaces import WorkspaceBackend, WorkspaceRef

from ..config import cfg

VENV_DIR = ".venv"
"""每个沙箱工作区根下的虚拟环境目录名"""

SANDBOX_HOME_DIR = ".home"
"""每个沙箱工作区根下的伪HOME目录名

受限进程以 `--clean-env` 启动，环境里只有运行时配置给的变量。不指一个可写 HOME 的话，
fontconfig 等的字体缓存没有落点，`expanduser("~") 也解析不到任何可用路径。
"""

FONT_DIR_CANDIDATES = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/etc/fonts",
    "/var/cache/fontconfig",
)
"""候选系统字体/字体配置目录"""


def _resolve_font_dirs() -> list[str]:
    """收集宿主上实际存在的字体目录，供沙箱只读授权"""
    candidates = [
        *FONT_DIR_CANDIDATES,
        str(Path.home() / ".local" / "share" / "fonts"),
        str(Path.home() / ".fonts"),
    ]
    dirs = [d for d in candidates if Path(d).is_dir()]
    if dirs:
        logger.info(f"沙箱已授权只读字体目录: {', '.join(dirs)}")
    else:
        logger.warning("宿主上未找到系统字体目录，沙箱内Python将无法使用系统字体")
    return dirs


ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
"""沙箱环境变量名的合法形态（防 shell 注入）"""

PACKAGE_SPEC_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:==|!=|<=|>=|~=)[A-Za-z0-9.+*!]+)?"
)
"""合法的包声明形态：`name` / `name[extras]` / `name==version`

包安装声明在**宿主侧**执行，必须从严，拒绝 URL、git、本地路径等一切可注入额外参数的形态。"""

LANDLOCK_REQUIRED_ABI = 6
"""sandlock 要求的最低 Landlock ABI（Linux 6.12+）"""

LANDLOCK_MIN_ABI = 4
"""低于此 ABI 连文件系统规则都保不住，必须报错而非降级"""

SYSTEM_READABLE_DIRS = ("/usr", "/lib", "/lib64", "/bin", "/etc", "/proc", "/dev")
"""降级自检探针随解释器根一并只读授权的系统目录"""

DEGRADED_PROTECTIONS = (
    "signal-scope",
    "abstract-unix-socket-scope",
    "fs-ioctl-dev",
)
"""ABI v5/v6 才有的三项 protection；降级后不再强制

对应能力：signal 宿主进程 / 连宿主 abstract unix socket / 设备 ioctl。
**文件系统隔离主线不受影响**（靠 Landlock FS rules + mirage VFS）。
"""

LANDLOCK_WRAPPER = """#!/bin/sh
# 注入 Landlock 降级参数后 exec 真正的 sandlock。
# 必须在 "--" 之前插入，否则会被当成被沙箱命令的参数。
sub="$1"; shift
exec "{real_sandlock}" "$sub" \\
    --allow-degraded signal-scope \\
    --allow-degraded abstract-unix-socket-scope \\
    --allow-degraded fs-ioctl-dev \\
    "$@"
"""


class SandlockUnavailableError(RuntimeError):
    """sandlock CLI 缺失或不可用"""


class LandlockUnavailableError(RuntimeError):
    """Landlock 不可用或 ABI 过低，无法保证文件系统隔离"""


class UvUnavailableError(RuntimeError):
    """uv CLI 缺失或不可用，沙箱无法提供 Python 环境"""


def _parse_abi(text: str) -> int | None:
    """从 `sandlock check` 输出里解析 Landlock ABI 版本号"""
    m = re.search(r"Landlock:\s*ABI\s*v(\d+)", text)
    return int(m.group(1)) if m else None


def check_sandlock() -> int:
    """校验 sandlock CLI 可用，返回宿主 Landlock ABI 版本

    缺失 CLI 或 Landlock 完全不可用即报错。
    """
    if shutil.which("sandlock") is None and not cfg.sandbox.landlock_real_binary:
        raise SandlockUnavailableError(
            "沙箱需要 sandlock CLI 在 PATH 上（https://github.com/multikernel/sandlock）。"
            "请安装后重启；如需关闭沙箱，可将 chat.sandbox.enabled 设为 false"
        )

    try:
        proc = subprocess.run(
            ["sandlock", "check"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise SandlockUnavailableError(f"执行 sandlock check 失败: {e}") from e

    output = f"{proc.stdout}\n{proc.stderr}"
    abi = _parse_abi(output)
    if abi is None:
        raise LandlockUnavailableError(
            f"无法从 sandlock check 输出解析 Landlock ABI，沙箱不可用:\n{output.strip()}"
        )
    if abi < LANDLOCK_MIN_ABI:
        raise LandlockUnavailableError(
            f"宿主 Landlock ABI 仅 v{abi}（需要 ≥ v{LANDLOCK_MIN_ABI}），"
            f"文件系统隔离无法保证，拒绝启动沙箱"
        )
    return abi


def ensure_landlock(bin_dir: Path) -> int | None:
    """按 ABI 情况决定是否生成降级 wrapper，返回宿主 ABI（未启用降级时为 None）"""
    abi = check_sandlock()
    mode = cfg.sandbox.landlock_degrade

    if mode == "strict":
        if abi < LANDLOCK_REQUIRED_ABI:
            raise LandlockUnavailableError(
                f"宿主 Landlock ABI v{abi} < 要求的 v{LANDLOCK_REQUIRED_ABI}，"
                f"且 landlock_degrade=strict（不降级）"
            )
        return None

    if abi >= LANDLOCK_REQUIRED_ABI and mode != "always":
        logger.info(f"Landlock ABI v{abi} 满足 sandlock 要求（v{LANDLOCK_REQUIRED_ABI}），无需降级")
        return None

    real = cfg.sandbox.landlock_real_binary or shutil.which("sandlock")
    if real is None:
        raise SandlockUnavailableError("找不到 sandlock 可执行文件，无法生成降级 wrapper")

    bin_dir.mkdir(parents=True, exist_ok=True)
    wrapper = bin_dir / "sandlock"
    wrapper.write_text(LANDLOCK_WRAPPER.format(real_sandlock=real), encoding="utf-8")
    wrapper.chmod(0o755)

    # 自检：确认降级确实生效
    python3 = shutil.which("python3")
    if python3 is None:
        raise SandlockUnavailableError("自检探针需要 PATH 上有 python3")
    # 授权「解释器所在根 + 系统根」。只授权解释器根不够：解释器是动态链接
    # ELF，内核还要以读权限打开 ld-linux/libc（merged-usr 下在 /usr 内）、
    # CPython 启动还要读 /dev/urandom，landlock 拒绝任何一个 execve 都直接
    # 返回 EACCES。uv run 会把 .venv/bin 前置到 PATH，which("python3") 拿到
    # 的是指向 uv 托管解释器的软链，resolve 后解释器根是 /home，极易漏掉
    # 系统根（交互 shell 直接 nb run 时 probe_root 恰为 /usr 才没暴露）。
    roots: list[str] = []
    for root in (f"/{Path(python3).resolve().parts[1]}", *SYSTEM_READABLE_DIRS):
        if root not in roots and Path(root).is_dir():
            roots.append(root)
    probe_argv = [str(wrapper), "run"]
    for root in roots:
        probe_argv += ["-r", root]
    probe_argv += ["--", python3, "-c", "print(1)"]
    probe = subprocess.run(
        probe_argv,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    combined = f"{probe.stdout}\n{probe.stderr}".lower()
    if probe.returncode != 0 or "protection unavailable" in combined:
        raise LandlockUnavailableError(
            f"Landlock 降级 wrapper 自检失败（returncode={probe.returncode}），"
            f"沙箱拒绝启动:\n{(probe.stdout + probe.stderr).strip()}"
        )

    reason = (
        f"Landlock ABI v{abi} < v{LANDLOCK_REQUIRED_ABI}"
        if abi < LANDLOCK_REQUIRED_ABI
        else "landlock_degrade=always"
    )
    logger.warning(
        f"{reason}，已启用降级 wrapper "
        f"({wrapper})。失去的保护：{'、'.join(DEGRADED_PROTECTIONS)}"
        f"（防进程逃逸维度）；文件系统隔离与内存限额不受影响。"
    )
    os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"
    logger.info(f"已将沙箱降级 wrapper 目录前置到 PATH: {bin_dir}")
    return abi


class KanadeWorkspace(MirageWorkspaceBackend):
    """mirage 官方 `MirageWorkspaceBackend` 的工作区根绑定版

    官方实现见 `mirage.agents.pydantic_ai.backend`：文件操作（read/write/
    ls 等）直通会话 VFS Ops 层、按**虚拟绝对路径**寻址，命令执行走
    `Session.shell()`（克隆语义，`cd`/`export` 不跨命令持久，超时已内建）。

    本项目采用 mirage「免 FUSE」布局：挂载前缀 = 宿主真实路径，虚拟路径与
    真实路径一致，sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。
    该布局下官方 backend 收到**相对路径**会落到 VFS 根 `/`（宿主系统视图的
    overlay）——读则 FileNotFoundError，写则**静默落空**（宿主工作区无文件）。
    这里把相对路径统一绑定到工作区根，与 `run` 的会话 cwd（初始化为工作区根）
    语义对齐。
    """

    def __init__(self, workspace: Workspace, root: str, session_id: str | None = None) -> None:
        super().__init__(workspace, session_id)
        self._root = root

    def _bind(self, path: str) -> str:
        """相对路径挂到工作区根，绝对路径原样归一化

        归一化后逃出工作区根的路径（`../..` 等）无需专门拦截：VFS 里只有
        工作区一个挂载，界外路径不存在，操作自然失败。
        """
        if not path.startswith("/"):
            path = posixpath.join(self._root, path)
        return posixpath.normpath(path)

    async def read_bytes(self, path: str) -> bytes:
        return await super().read_bytes(self._bind(path))

    async def write_bytes(self, path: str, data: bytes) -> None:
        await super().write_bytes(self._bind(path), data)

    async def stat(self, path: str):
        return await super().stat(self._bind(path))

    async def list_dir(self, path: str):
        return await super().list_dir(self._bind(path))

    async def make_dir(self, path: str) -> None:
        await super().make_dir(self._bind(path))

    async def exists(self, path: str) -> bool:
        return await super().exists(self._bind(path))

    async def remove(self, path: str) -> None:
        await super().remove(self._bind(path))

    async def realpath(self, path: str) -> str:
        return await super().realpath(self._bind(path))


def sandbox_runtime_config(
    workspace_dir: Path,
    uv_python_dir: Path | None,
    font_dirs: tuple[str, ...] = (),
) -> SandlockConfig:
    """sandlock 受限子进程的统一授权与环境配置

    只授予工作区、uv 解释器目录与字体目录，之外的宿主路径 sandlock 一律拒绝。
    """
    readable = [str(workspace_dir)]
    if uv_python_dir is not None:
        # venv 内的解释器是指向 uv 托管解释器的符号链接，需要只读授权
        readable.append(str(uv_python_dir))
    readable.extend(font_dirs)
    sandbox_home = workspace_dir / SANDBOX_HOME_DIR
    return SandlockConfig(
        fs_readable=tuple(readable),
        fs_writable=(str(workspace_dir),),
        max_memory=cfg.sandbox.memory_limit,
        env={
            # 受限子进程的 PATH 只含 venv bin，不提供系统 python3；
            "PATH": str(workspace_dir / VENV_DIR / "bin"),
            # HOME/XDG 指向工作区内可写目录，字体缓存才有落点
            "HOME": str(sandbox_home),
            "XDG_CACHE_HOME": str(sandbox_home / ".cache"),
            "XDG_CONFIG_HOME": str(sandbox_home / ".config"),
        },
    )


class SandboxWorkspaceCapability(AbstractCapability[AgentDepsT]):
    """按运行时分发沙箱工作区的 capability"""

    def get_workspace(
        self, ctx: RunContext[AgentDepsT], *, ref: WorkspaceRef | None
    ) -> WorkspaceBackend | None:
        # mirage 官方 `MirageWorkspace` capability 持有单个固定 workspace，而本项目
        # 每个聊天会话一个沙箱。这里在 `get_workspace` 时从`ctx.deps.sandbox`
        # 解析当前会话的沙箱，返回其根绑定 backend。
        sandbox: SandboxSession | None = getattr(ctx.deps, "sandbox", None)
        if sandbox is None or sandbox.closed:
            return None
        return sandbox.backend


@dataclass
class SandboxSession:
    """沙箱：workspace + backend"""

    workspace_dir: Path
    workspace: Workspace
    backend: KanadeWorkspace
    session_id: str
    uv_bin: str | None = None
    """宿主侧 uv 可执行文件路径"""

    uv_python_dir: Path | None = None
    """uv 托管解释器目录"""

    host_python: str | None = None
    """宿主系统 python3 路径"""

    font_dirs: tuple[str, ...] = ()
    """额外只读授权的宿主字体目录"""

    _closed: bool = field(default=False, init=False, repr=False)
    _venv_seq: int = field(default=0, init=False, repr=False)
    """已注册的 venv-bin 运行时计数"""

    _venv_cmds: set[str] = field(default_factory=set, init=False, repr=False)
    """已注册进沙箱路由的 venv bin 命令名"""

    @property
    def root(self) -> str:
        """工作区根的绝对路径"""
        return str(self.workspace_dir)

    @property
    def closed(self) -> bool:
        return self._closed

    async def close(self) -> None:
        """关闭工作区（幂等）。工作区文件保留在宿主目录，仅丢弃 shell 会话状态"""
        if self._closed:
            return
        self._closed = True
        try:
            await self.workspace.close()
        except Exception as e:
            logger.warning(f"关闭沙箱工作区时发生错误: {e}")

    # --- 工具层使用

    def _abs(self, path: Path) -> str:
        """相对路径挂到工作区根的虚拟路径"""
        p = str(path)
        return p if p.startswith("/") else posixpath.join(self.root, p)

    async def read(self, path: Path) -> BytesIO:
        return BytesIO(await self.workspace.vfs.read(self._abs(path), session_id=self.session_id))

    async def write(self, path: Path, data: BytesIO) -> None:
        """写文件（可覆盖），自动逐级创建缺失的父目录"""
        content = data.read()
        target = self._abs(path)
        current = ""
        for part in [p for p in posixpath.dirname(target).split("/") if p]:
            current = f"{current}/{part}"
            try:
                await self.workspace.vfs.mkdir(current, session_id=self.session_id)
            except FileExistsError:
                continue
        await self.workspace.vfs.write(target, content, session_id=self.session_id)

    # ===== python虚拟环境

    @property
    def venv_dir(self) -> Path:
        """工作区虚拟环境目录"""
        return self.workspace_dir / VENV_DIR

    @property
    def venv_bin_dir(self) -> Path:
        """虚拟环境 bin 目录"""
        return self.venv_dir / "bin"

    def _venv_commands(self) -> set[str]:
        """虚拟环境 bin 下的现有命令名"""
        try:
            return {p.name for p in self.venv_bin_dir.iterdir()}
        except OSError:
            return set()

    async def _run_host(self, argv: list[str]) -> tuple[int, str]:
        """宿主侧执行命令，返回 (exit_code, 合并输出尾部)"""
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.workspace_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as e:
            return 1, f"无法启动 {argv[0]}: {e}"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=cfg.sandbox.venv_timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return (
                1,
                f"命令超时（>{cfg.sandbox.venv_timeout}s）: {' '.join(argv[:3])} …",
            )
        text = (stdout + b"\n" + stderr).decode("utf-8", "replace").strip()
        return proc.returncode if proc.returncode is not None else 1, text[-2000:]

    def _register_venv_commands(self):
        """把 venv bin 中尚未注册的命令动态挂进沙箱路由"""
        to_register = tuple(sorted(self._venv_commands() - self._venv_cmds))
        if not to_register:
            return []
        self._venv_seq += 1
        runtime_cls = type(
            f"_VenvBinRuntime{self._venv_seq}",
            (SandlockRuntime,),
            {"name": f"venv-bin-{self._venv_seq}"},
        )
        self.workspace.add_runtime(
            runtime_cls(
                captures=to_register,
                config=sandbox_runtime_config(
                    self.workspace_dir, self.uv_python_dir, self.font_dirs
                ),
            )
        )
        self._venv_cmds.update(to_register)
        return to_register

    async def setup_venv(
        self, python_version: str | None = None, packages: list[str] | None = None
    ) -> str:
        """宿主侧创建/更新工作区虚拟环境并安装包

        1. 优先用 uv
        2. 无 uv 时回退原生方式：宿主 `python3 -m venv` + venv 内 pip。

        返回给模型的结果文本。
        """
        uv_bin = self.uv_bin
        host_python = self.host_python
        if uv_bin is None and host_python is None:
            return "uv 与宿主 python3 均不可用，无法创建 Python 环境。"
        use_uv = uv_bin is not None

        if packages:
            invalid = [p for p in packages if not PACKAGE_SPEC_RE.fullmatch(p)]
            if invalid:
                return (
                    f"不合法的包声明: {invalid}。仅接受 `name`、`name[extras]`、"
                    "`name==version` 等纯文本形态，不支持 URL/git/本地路径"
                )

        report: list[str] = []
        if not self.venv_dir.exists():
            if use_uv:
                version = python_version or cfg.sandbox.venv_python
                code, output = await self._run_host(
                    [uv_bin, "venv", "--python", version, str(self.venv_dir)]
                )
                if code != 0:
                    return f"创建虚拟环境失败:\n{output}"
                report.append(f"已创建虚拟环境 {VENV_DIR}/（uv，Python {version}）")
            else:
                # 原生回退：保持默认 symlink 形态
                # 不能用 --copies，真文件解释器在沙箱内会踩 sandlock 的 readlink bug 启动崩溃
                if host_python is None:
                    return "宿主 python3 不可用，无法创建 Python 环境。"
                interpreter = host_python
                if python_version:
                    candidate = shutil.which(f"python{python_version}")
                    if candidate is not None:
                        interpreter = candidate
                    else:
                        report.append(f"未找到宿主 python{python_version}，已用默认回退解释器")
                code, output = await self._run_host([interpreter, "-m", "venv", str(self.venv_dir)])
                if code != 0:
                    # Debian/Ubuntu 拆包后系统 python 无 ensurepip，需 python3-venv：
                    # 降级建无 pip 的裸环境，至少标准库可用
                    shutil.rmtree(self.venv_dir, ignore_errors=True)
                    code, output = await self._run_host(
                        [interpreter, "-m", "venv", "--without-pip", str(self.venv_dir)]
                    )
                    if code != 0:
                        return f"创建虚拟环境失败:\n{output}"
                    report.append(f"已创建虚拟环境 {VENV_DIR}/（python -m venv --without-pip）")
                else:
                    report.append(f"已创建虚拟环境 {VENV_DIR}/（python -m venv）")
        elif python_version:
            report.append("虚拟环境已存在，忽略 python_version 参数")

        if packages:
            before = self._venv_commands()
            venv_python = str(self.venv_bin_dir / "python")
            if use_uv:
                code, output = await self._run_host(
                    [
                        uv_bin,
                        "pip",
                        "install",
                        # "--no-build",  # 只装 wheel，避免 sdist 构建钩子在宿主执行任意代码
                        "--python",
                        venv_python,
                        *packages,
                    ]
                )
            else:
                # 原生回退：`python3 -m venv` 默认自带 pip（ensurepip）；
                # --without-pip 降级创建的环境需先补装
                if "pip" not in before:
                    code, output = await self._run_host(
                        [venv_python, "-m", "ensurepip", "--upgrade"]
                    )
                    if code != 0:
                        return (
                            "安装包失败：venv 内无 pip 且 ensurepip 不可用。"
                            "请要求宿主安装 uv（推荐）或 python3-venv 后重试"
                        )
                code, output = await self._run_host(
                    [
                        venv_python,
                        "-m",
                        "pip",
                        "install",
                        "--disable-pip-version-check",
                        "--no-input",
                        # "--only-binary", ":all:",  # 等价 uv --no-build
                        *packages,
                    ]
                )
            if code != 0:
                return f"安装包失败:\n{output}"
            new_cmds = self._venv_commands() - before
            report.append(f"已安装 {len(packages)} 个包: {' '.join(packages)}")
            if new_cmds:
                report.append(f"新增可执行命令: {' '.join(sorted(new_cmds))}")

        # 全量补注册
        registered = self._register_venv_commands()
        if registered:
            report.append(f"已注册沙箱命令: {' '.join(registered)}")

        # 更新会话 PATH：venv 优先，附带系统工具目录。
        # 系统目录到这里才引入：venv 已存在，python3 必然解析到 venv 解释器。
        io = await self.workspace.shell(
            f"export PATH={shlex.quote(str(self.venv_bin_dir))}:/usr/bin:/bin",
            session_id=self.session_id,
        )
        if io.exit_code != 0:
            stderr = (await io.materialize_stderr()).decode("utf-8", "replace")
            report.append(f"警告：更新会话 PATH 失败: {stderr.strip()}")

        report.append("python3 现在可用。需要更多包时再次调用本工具。")
        return "\n".join(report)


class SandboxManager:
    """沙箱管理器：为每个会话维护一个常驻沙箱，工作区目录按会话管理"""

    def __init__(self):
        self._bin_dir: Path | None = None

        self._root_dir = cfg.sandbox.workspace_dir_path.resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)

        self._sessions: dict[str, SandboxSession] = {}
        """各会话的常驻沙箱，键为会话ID"""

        self._uv_bin: str | None = None
        """宿主侧 uv 可执行文件路径"""

        self._uv_python_dir: Path | None = None
        """额外只读授权的解释器目录"""

        self._host_python: str | None = None
        """宿主系统 python3 路径"""

        self._font_dirs: tuple[str, ...] = tuple(_resolve_font_dirs())
        """授权沙箱只读的宿主字体目录"""

        driver = get_driver()
        driver.on_startup(self._startup)

    # ===== 路径 =====

    @staticmethod
    def _safe_name(session_id: str) -> str:
        """会话ID转单一安全路径段"""
        name = session_id.replace("/", "_").replace("\\", "_")
        if name in {"", ".", ".."}:
            raise ValueError(f"会话ID无法转换为安全目录名: {session_id!r}")
        return name

    def _workspace_dir(self, session_id: str) -> Path:
        """会话对应的宿主工作区目录"""
        return (self._root_dir / self._safe_name(session_id)).resolve()

    def delete_workspace(self, session_id: str) -> None:
        """删除会话的工作区目录"""
        workspace_dir = self._workspace_dir(session_id)
        if workspace_dir.parent != self._root_dir:
            logger.warning(f"拒绝删除沙箱目录（不在根目录下）: {workspace_dir}")
            return
        shutil.rmtree(workspace_dir, ignore_errors=True)

    # ===== mirage 运行时 =====

    def _check_uv(self) -> None:
        """校验 uv 可用并解析托管解释器目录"""
        resolved = shutil.which(cfg.sandbox.uv_bin)
        if resolved is None:
            raise UvUnavailableError(
                f"uv CLI 不可用（{cfg.sandbox.uv_bin}）"
                "（https://docs.astral.sh/uv/）。"
                "可将 chat.sandbox.uv_bin 指向 uv 绝对路径，"
                "否则 Python 环境回退到 python -m venv"
            )

        if cfg.sandbox.uv_python_dir:
            python_dir = Path(cfg.sandbox.uv_python_dir)
        else:
            try:
                proc = subprocess.run(
                    [resolved, "python", "dir"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as e:
                raise UvUnavailableError(f"执行 `uv python dir` 失败: {e}") from e
            if proc.returncode != 0:
                raise UvUnavailableError(
                    f"解析 uv 托管解释器目录失败:\n{(proc.stdout + proc.stderr).strip()}"
                )
            python_dir = Path(proc.stdout.strip())
        if not python_dir.is_dir():
            raise UvUnavailableError(f"uv 托管解释器目录不存在: {python_dir}")
        self._uv_bin = resolved
        self._uv_python_dir = python_dir

    def _runtime_config(self, workspace_dir: Path) -> SandlockConfig:
        """sandlock 运行时的统一授权配置"""
        return sandbox_runtime_config(workspace_dir, self._uv_python_dir, self._font_dirs)

    def _build_runtime(self, workspace_dir: Path) -> SandlockRuntime:
        """构造 sandlock 运行时"""
        return SandlockRuntime(
            captures=("python3", "python"),
            config=self._runtime_config(workspace_dir),
        )

    # ===== 创建 =====

    async def create(self, session_id: str) -> SandboxSession:
        """获取会话的常驻沙箱，不存在时创建"""
        session = self._sessions.get(session_id)
        if session is not None and not session.closed:
            return session
        session = await self._create(session_id)
        self._sessions[session_id] = session
        logger.info(f"已为会话{session_id}创建常驻沙箱（工作区{session.root}）")
        return session

    async def close(self, session_id: str) -> None:
        """关闭会话的常驻沙箱（未创建时为空操作）。工作区文件保留在宿主目录"""
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await session.close()

    async def close_all(self) -> None:
        """关闭所有常驻沙箱"""
        sessions = [self._sessions.pop(sid) for sid in list(self._sessions)]
        for session in sessions:
            await session.close()

    def workspace_root(self, session_id: str) -> str:
        """会话工作区根的绝对路径"""
        return str(self._workspace_dir(session_id))

    async def _create(self, session_id: str) -> SandboxSession:
        """构建 Workspace + mirage 会话 + 官方 Pydantic AI backend

        挂载前缀 = DiskVFS 自己的宿主 realpath：虚拟路径与真实路径一致，
        sandlock 拉起的 native 进程无需 FUSE 即可读写同一批文件。

        会话初始化（cd + export）走 shell 命令：不传 per-call `cwd` 时命令
        直接跑在持久 session 上，`cd`/`export` 会留在 session 状态里，
        之后的 `execute` 即以工作区根为 cwd、带配置的环境变量。
        """
        workspace_dir = self._workspace_dir(session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)

        sandbox_home = workspace_dir / SANDBOX_HOME_DIR
        for sub in (sandbox_home, sandbox_home / ".cache", sandbox_home / ".config"):
            sub.mkdir(parents=True, exist_ok=True)

        workspace = Workspace(
            {
                str(workspace_dir): (
                    DiskVFS(str(workspace_dir)),
                    MountMode.EXEC,
                )
            },
            mode=MountMode.EXEC,
            runtimes=[self._build_runtime(workspace_dir)],
        )
        mirage_session = f"kanade-{self._safe_name(session_id)}"
        workspace.create_session(mirage_session)

        init_parts = [f"cd {shlex.quote(str(workspace_dir))}"]
        for key, value in cfg.sandbox.environment.items():
            if not ENV_KEY_RE.fullmatch(key):
                await workspace.close()
                raise ValueError(f"沙箱环境变量名不合法: {key!r}")
            init_parts.append(f"export {key}={shlex.quote(value)}")

        io = await workspace.shell("; ".join(init_parts), session_id=mirage_session)
        if io.exit_code != 0:
            stderr = (await io.materialize_stderr()).decode("utf-8", "replace")
            await workspace.close()
            raise RuntimeError(f"沙箱会话初始化失败（cd/export）: {stderr.strip()}")

        return SandboxSession(
            workspace_dir=workspace_dir,
            workspace=workspace,
            backend=KanadeWorkspace(
                workspace=workspace,
                root=str(workspace_dir),
                session_id=mirage_session,
            ),
            session_id=mirage_session,
            uv_bin=self._uv_bin,
            uv_python_dir=self._uv_python_dir,
            host_python=self._host_python,
            font_dirs=self._font_dirs,
        )

    # ===== 生命周期 =====

    async def _startup(self):
        self._bin_dir = self._root_dir / ".bin"
        ensure_landlock(self._bin_dir)
        try:
            self._check_uv()
        except UvUnavailableError as e:
            logger.warning(f"{e}")
        self._host_python = self._resolve_host_python()
        if self._uv_bin is None and self._host_python is None:
            logger.warning("宿主侧既无 uv 也无 python3，沙箱内将无法创建 Python 环境")
        logger.info(
            f"mirage沙箱已就绪，工作区根目录: {self._root_dir}，"
            f"uv: {self._uv_bin}，解释器目录: {self._uv_python_dir}，"
            f"回退python: {self._host_python}"
        )

    def _resolve_host_python(self) -> str | None:
        """解析无 uv 时的原生回退解释器

        优先 `python{venv_python}`（若在 PATH 上，常为带完整 ensurepip/pip
        的 uv 托管或自装解释器），否则退回系统 python3。若选中解释器位于
        系统目录之外（沙箱未授权），把其安装根补进只读授权，保证 venv 符号链接在沙箱内可执行。
        """
        interpreter = shutil.which(f"python{cfg.sandbox.venv_python}") or shutil.which("python3")
        if interpreter is None:
            return None
        if self._uv_python_dir is None:
            install_root = Path(interpreter).resolve().parent.parent
            if str(install_root) not in ("/", "/usr", "/usr/local"):
                self._uv_python_dir = install_root
        return interpreter
