"""沙箱环境工具：创建 Python 虚拟环境"""

from nonebot import logger
from pydantic_ai import RunContext

from ..deps import ChatDeps
from .common import get_sandbox


async def setup_python_env(
    ctx: RunContext[ChatDeps],
    packages: list[str] | None = None,
    python_version: str = "",
) -> str:
    """在沙箱工作区创建 Python 虚拟环境并安装包。

    沙箱内默认没有可用的 python3，必须先调用本工具创建 .venv 后 python3 才可用。
    已有虚拟环境时仅安装新包。沙箱内无法联网装包，需要任何第三方包时都用本工具。
    安装自带命令行工具的包后，该命令可在沙箱中直接调用（也可用 `python3 -m 模块名`）。

    Args:
        packages: 要安装的包声明列表，如 ["numpy", "requests>=2.31"]；留空仅创建环境。
        python_version: 创建环境时使用的 Python 版本（如 "3.13"），仅首次创建有效，
            取决于宿主可用的解释器；留空用默认。
    """
    sandbox = get_sandbox(ctx)
    if sandbox is None:
        return "未启用沙箱，无法创建 Python 环境。"
    logger.info("创建沙箱Python环境，包列表: {}", packages)
    return await sandbox.setup_venv(
        python_version=python_version or None,
        packages=packages,
    )
