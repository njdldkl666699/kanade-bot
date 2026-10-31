"""consume_crystal：按量扣减水晶（Token计费）允许扣至负数"""

import asyncio
import importlib

import nonebot

nonebot.init(_env_file=None)


def _load_plugin(name: str) -> None:
    if nonebot.get_plugin(name.rsplit(".", 1)[-1]) is None:
        assert nonebot.load_plugin(name)


_load_plugin("nonebot_plugin_localstore")
_load_plugin("kanade_bot.plugins.model_updater")
_load_plugin("kanade_bot.plugins.command_counter")
_load_plugin("nonebot_plugin_apscheduler")
_load_plugin("kanade_bot.plugins.crystal")

crystal = importlib.import_module("kanade_bot.plugins.crystal.crystal")


def test_consume_crystal_allows_negative_balance(monkeypatch):
    data = {"user": 30}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    dirty_calls: list[int] = []
    monkeypatch.setattr(
        crystal.crystal_data_writer,
        "mark_dirty",
        lambda: dirty_calls.append(1),
    )

    crystal.consume_crystal("onebot", "user", 48)

    assert data["user"] == -18
    assert dirty_calls == [1]


def test_consume_crystal_initializes_missing_user(monkeypatch):
    data = {}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    monkeypatch.setattr(crystal.crystal_data_writer, "mark_dirty", lambda: None)

    crystal.consume_crystal("onebot", "new_user", 5)

    assert data["new_user"] == -5


def test_check_user_crystal_str_config_threshold_is_positive(monkeypatch):
    data = {"rich": 10, "poor": 0}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    original_consumes = crystal.crystal_config.instance.handler_consumes
    crystal.crystal_config.instance.handler_consumes = {crystal.HandlerKeyEnum.CHAT: "按Token计费"}

    assert crystal.check_user_crystal(crystal.HandlerKeyEnum.CHAT, "onebot", "rich") is True
    assert crystal.check_user_crystal(crystal.HandlerKeyEnum.CHAT, "onebot", "poor") is False

    crystal.crystal_config.instance.handler_consumes = original_consumes


class FakeMatcher:
    def __init__(self):
        self.sent: list[str] = []

    async def send(self, message, **kwargs):
        self.sent.append(str(message))


def test_check_user_crystal_maybe_init_registers_new_user(monkeypatch):
    data = {}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    monkeypatch.setattr(crystal.crystal_data_writer, "mark_dirty", lambda: None)
    original_consumes = crystal.crystal_config.instance.handler_consumes
    crystal.crystal_config.instance.handler_consumes = {crystal.HandlerKeyEnum.GACHA: 100}

    matcher = FakeMatcher()
    result = asyncio.run(
        crystal.check_user_crystal_maybe_init(
            matcher, crystal.HandlerKeyEnum.GACHA, "onebot", "new_user"
        )
    )

    # 新号自动注册，赠送首次使用水晶并发送消息，余额足够通过检查
    bonus = crystal.crystal_config.instance.first_use_bonus
    assert result is True
    assert data["new_user"] == bonus
    assert matcher.sent

    crystal.crystal_config.instance.handler_consumes = original_consumes


def test_check_user_crystal_maybe_init_skips_existing_user(monkeypatch):
    data = {"old_user": 1000}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    monkeypatch.setattr(crystal.crystal_data_writer, "mark_dirty", lambda: None)
    original_consumes = crystal.crystal_config.instance.handler_consumes
    crystal.crystal_config.instance.handler_consumes = {crystal.HandlerKeyEnum.GACHA: 100}

    matcher = FakeMatcher()
    result = asyncio.run(
        crystal.check_user_crystal_maybe_init(
            matcher, crystal.HandlerKeyEnum.GACHA, "onebot", "old_user"
        )
    )

    # 老用户不再注册、不发首次使用消息，余额不受影响
    assert result is True
    assert data["old_user"] == 1000
    assert not matcher.sent

    crystal.crystal_config.instance.handler_consumes = original_consumes
