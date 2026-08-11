from pathlib import Path
from typing import Any

import anyconfig
from nonebot.config import Env
from nonebot.utils import deep_update


def load_configs(
    working_dir: Path,
    stem: str = "config",
    suffix: str = ".yaml",
) -> tuple[Env, dict[str, Any]]:
    """加载配置文件，并根据environment字段加载对应的环境配置

    Args:
        working_dir: 配置文件所在的工作目录
        stem: 配置文件名的前缀，默认为"config"
        suffix: 配置文件名的后缀，默认为".yaml"

    Returns:
        env: 环境对象
        configs: 合并后的配置字典

    Raises:
        FileNotFoundError: 配置文件不存在
        TypeError: 配置文件解析结果不是字典类型
    """
    path = working_dir / f"{stem}{suffix}"
    if not path.exists():
        raise FileNotFoundError(f"配置文件 {path} 不存在")

    configs = anyconfig.load(path)
    if not isinstance(configs, dict):
        raise TypeError(f"配置文件 {path} 解析结果不是字典类型: {type(configs)}")

    if environment := configs.get("environment"):
        env = Env(environment=environment)
    else:
        env = Env()

    env_config_path = working_dir / f"{stem}-{env.environment}{suffix}"
    env_configs = anyconfig.load(env_config_path)
    if not isinstance(env_configs, dict):
        raise TypeError(f"环境配置文件 {env_config_path} 解析结果不是字典类型: {type(env_configs)}")

    # 递归（深度）合并字典
    return env, deep_update(configs, env_configs)
