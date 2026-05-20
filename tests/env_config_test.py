from typing import TYPE_CHECKING, TypeVar

from nonebot import get_plugin_config
from nonebot.compat import PYDANTIC_V2, model_dump, type_validate_python
from nonebot.config import DOTENV_TYPE, BaseSettings, Env, SettingsConfig
from pydantic import BaseModel
from nonebot.plugin import C


class TestConfig(BaseSettings):
    """测试配置类，继承自BaseSettings，可以在测试中使用这个类来加载环境变量"""

    if TYPE_CHECKING:
        _env_file: DOTENV_TYPE | None = ".env", ".env.prod"

    test_var: str = "default"
    """测试环境变量，默认为"default"，可以在测试中通过设置环境变量来覆盖这个值"""

    if PYDANTIC_V2:  # pragma: pydantic-v2
        model_config = SettingsConfig(env_file=(".env", ".env.prod"))
    else:  # pragma: pydantic-v1

        class Config(  # pyright: ignore[reportIncompatibleVariableOverride]
            SettingsConfig
        ):
            env_file = ".env", ".env.prod"


class ScopedConfig(BaseModel):
    var2: str = "default2"


class TestScopedConfig(TestConfig):
    test: ScopedConfig


def get_config(global_config: TestConfig, config: type[C]) -> C:
    """从全局配置获取当前插件需要的配置项。"""
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


if __name__ == "__main__":
    # 直接运行这个文件时，打印测试配置的值
    env = Env()
    _env_file = f".env.{env.environment}"
    config = TestConfig(_env_file=(".env", _env_file))
    print(f"Test variable value: {config.test_var}")
    # 能读到

    # 还可以测试嵌套配置
    scoped_config = get_config(config, TestScopedConfig)
    print(f"Scoped variable value: {scoped_config.test.var2}")
