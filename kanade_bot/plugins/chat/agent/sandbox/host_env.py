"""沙箱宿主环境：sandlock/Landlock 检查、降级 wrapper、uv 与宿主 Python 解析"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from nonebot import logger

from ...config import cfg

VENV_DIR = ".venv"
"""每个沙箱工作区根下的虚拟环境目录名"""

SANDBOX_HOME_DIR = ".home"
"""每个沙箱工作区根下的伪HOME目录名"""

FONT_DIR_CANDIDATES = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/etc/fonts",
    "/var/cache/fontconfig",
)
"""候选系统字体/字体配置目录"""


def resolve_font_dirs() -> list[str]:
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
"""ABI v5/v6 才有的三项 protection；降级后不再强制"""

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


def check_uv() -> tuple[str, Path]:
    """校验 uv 可用并解析托管解释器目录

    :returns: (uv 可执行文件路径, 托管解释器目录)；不可用时抛 `UvUnavailableError`
    """
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
    return resolved, python_dir


def resolve_host_python(uv_python_dir: Path | None) -> tuple[str | None, Path | None]:
    """解析无 uv 时的原生回退解释器；其安装根在系统目录外时补进只读授权

    :returns: (解释器路径, 生效的只读解释器目录)；无解释器时首项为 None，
        次项原样返回传入的 `uv_python_dir`。
    """
    interpreter = shutil.which(f"python{cfg.sandbox.venv_python}") or shutil.which("python3")
    if interpreter is None:
        return None, uv_python_dir
    effective_dir = uv_python_dir
    if uv_python_dir is None:
        install_root = Path(interpreter).resolve().parent.parent
        if str(install_root) not in ("/", "/usr", "/usr/local"):
            effective_dir = install_root
    return interpreter, effective_dir
