from typing import TYPE_CHECKING

from nonebot.compat import PYDANTIC_V2, model_dump, type_validate_python
from nonebot.config import DOTENV_TYPE, BaseSettings, SettingsConfig
from nonebot.plugin import C


class Config(BaseSettings):
    """配置类，继承自BaseSettings，可以在脚本中使用这个类来加载环境变量"""

    if TYPE_CHECKING:
        _env_file: DOTENV_TYPE | None = ".env", ".env.prod"

    if PYDANTIC_V2:  # pragma: pydantic-v2
        model_config = SettingsConfig(env_file=(".env", ".env.prod"))  # pyright: ignore[reportCallIssue]
    else:  # pragma: pydantic-v1

        class Config(  # pyright: ignore[reportIncompatibleVariableOverride]
            SettingsConfig
        ):
            env_file = ".env", ".env.prod"


global_config = Config()
"""全局配置对象，加载了环境变量，可用使用get_config函数从这个对象中获取脚本需要的配置项。"""


def get_config(config: type[C]) -> C:
    """从全局配置获取当前脚本需要的配置项。"""
    return type_validate_python(
        config,
        BaseSettings._settings_build_values(
            config,
            model_dump(global_config),
            env_file=global_config._env_file,
            env_file_encoding=global_config._env_file_encoding,
            env_nested_delimiter=global_config._env_nested_delimiter,
        ),
    )
