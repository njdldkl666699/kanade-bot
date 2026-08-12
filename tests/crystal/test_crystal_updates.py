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


def test_consume_and_increment_changes_balance_once(monkeypatch):
    data = {"user": 100}
    monkeypatch.setattr(
        type(crystal.crystal_data.instance),
        "get_by_platform",
        lambda _self, _platform: data,
    )
    original_consumes = crystal.crystal_config.instance.handler_consumes
    crystal.crystal_config.instance.handler_consumes = {crystal.HandlerKeyEnum.GACHA: 20}
    dirty_calls: list[int] = []
    monkeypatch.setattr(
        crystal.crystal_data_writer,
        "mark_dirty",
        lambda: dirty_calls.append(1),
    )

    crystal.consume_and_increment(crystal.HandlerKeyEnum.GACHA, "onebot", "user", 7)

    assert data["user"] == 87
    assert dirty_calls == [1]
    crystal.crystal_config.instance.handler_consumes = original_consumes
