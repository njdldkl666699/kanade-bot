"""沙箱会话：常驻 Workspace + 工作区文件读写 + Python 虚拟环境管理"""

import asyncio
import posixpath
import re
import shlex
import shutil
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from mirage import Workspace
from mirage.runtime.sandbox.sandlock import SandlockRuntime
from nonebot import logger

from ...config import cfg
from .host_env import VENV_DIR
from .workspace import KanadeWorkspace, sandbox_runtime_config

PACKAGE_SPEC_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._-]*"
    r"(?:\[[A-Za-z0-9._,-]+\])?"
    r"(?:(?:==|!=|<=|>=|~=)[A-Za-z0-9.+*!]+)?"
)
"""合法的包声明形态：`name` / `name[extras]` / `name==version`"""


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
                # 原生回退：保持默认 symlink 形态即可，无需 --copies
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

        # 更新会话 PATH：系统工具目录此时才引入（python3 已必然解析到 venv 解释器）
        io = await self.workspace.shell(
            f"export PATH={shlex.quote(str(self.venv_bin_dir))}:/usr/bin:/bin",
            session_id=self.session_id,
        )
        if io.exit_code != 0:
            stderr = (await io.materialize_stderr()).decode("utf-8", "replace")
            report.append(f"警告：更新会话 PATH 失败: {stderr.strip()}")

        report.append("python3 现在可用。需要更多包时再次调用本工具。")
        return "\n".join(report)
