"""mirage沙箱配置"""

from pathlib import Path
from typing import Literal

from nonebot import require

from kanade_bot.utils.schema import AttrDocModel

require("nonebot_plugin_localstore")
from nonebot_plugin_localstore import get_plugin_data_file


class SandboxConfig(AttrDocModel):
    """mirage沙箱配置"""

    enabled: bool = False
    """是否启用沙箱（文件与shell能力）

    需要 sandlock CLI 在 PATH 上"""

    environment: dict[str, str] = {}
    """沙箱环境变量，注入到每个会话

    注意：这些变量对工作区内的所有进程可见，不要在此存放敏感密钥"""

    memory_limit: str = "512M"
    """sandlock受限子进程的内存上限"""

    workspace_dir: str = "sandboxes/"
    """沙箱工作区根目录名，位于插件缓存目录；每个聊天会话一个子目录"""

    landlock_degrade: Literal["auto", "always", "strict"] = "auto"
    """Landlock ABI 不足时的策略

    - `auto`（默认）：按 `sandlock check` 的结果自动决定，
      ABI < v6 时启用wrapper 注入 `--allow-degraded`
    - `always`：始终注入降级参数
    - `strict`：ABI 不足 v6 即报错"""

    landlock_real_binary: str | None = None
    """真实 sandlock 可执行文件的绝对路径

    默认：生成 wrapper 时用 `shutil.which("sandlock")` 动态求值，
    适配各部署环境不同的安装路径。仅当 sandlock 不在 PATH 时才需显式指定。"""

    uv_bin: str = "uv"
    """uv 可执行文件，PATH 上的名字或绝对路径

    不可用时自动回退到宿主 `python3 -m venv` 原生方式"""

    uv_python_dir: str | None = None
    """uv 托管解释器目录

    默认启动时执行 `uv python dir` 动态求值。"""

    venv_python: str = "3.13"
    """沙箱虚拟环境默认使用的 Python 版本

    uv 方式下可为 uv 托管解释器任意版本；原生回退方式下按 `python{版本}`
    在宿主查找（如 python3.13），找不到时用宿主默认 python3"""

    venv_timeout: int = 300
    """宿主侧执行 uv 命令的超时秒数"""

    @property
    def workspace_dir_path(self) -> Path:
        return get_plugin_data_file(self.workspace_dir)
