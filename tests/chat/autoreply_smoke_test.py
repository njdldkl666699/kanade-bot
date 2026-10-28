"""主动回复重构冒烟：防抖状态机 / 时段窗口匹配 / 决策历史持久化往返 / 插件加载链

直接运行：`uv run python tests/nonebot/autoreply_smoke_test.py`
"""

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path
from time import monotonic
from zoneinfo import ZoneInfo

from bot import init_nonebot, register_adapters_and_load_plugins
from scripts.util import load_configs

# 复用生产初始化路径（config.yaml + config-{env}.yaml 合并；不 run 不监听）
_env, _configs = load_configs(Path("."))
init_nonebot(env=_env, **_configs)
register_adapters_and_load_plugins()

from kanade_bot.plugins.chat.agent import proactive as rg
from kanade_bot.plugins.chat.agent.history import GateStore, SessionStore
from kanade_bot.plugins.chat.agent.proactive.debounce import MessageDebouncer

TZ = ZoneInfo("Asia/Shanghai")


def test_debouncer():
    from kanade_bot.plugins.chat.agent.proactive.debounce import (
        BATCH_MAX_WAIT,
        NEW_AUTHOR_WAIT,
        OPEN_ACTIVE_WAIT,
        OPEN_QUIET_WAIT,
        SAME_AUTHOR_WAIT,
    )

    d = MessageDebouncer()
    t0 = monotonic()
    d.observe("u1", now=t0)
    assert d.batch_size == 1
    assert d.deadline() == t0 + OPEN_QUIET_WAIT, "安静开批等待"

    d.observe("u1", now=t0 + 1.0)
    assert d.deadline() == t0 + 1.0 + SAME_AUTHOR_WAIT, "同人续发等待"

    d.observe("u2", now=t0 + 1.5)
    assert d.deadline() == t0 + 1.5 + NEW_AUTHOR_WAIT, "换人续发等待"

    # 整批上限钳制：消息持续越过 first_at + BATCH_MAX_WAIT - SAME_AUTHOR_WAIT
    # （此刻起 now + wait 才会触及上限），步长按常量推导不随调参失效
    clamp_at = BATCH_MAX_WAIT - SAME_AUTHOR_WAIT
    steps = 50
    for i in range(steps):
        d.observe("u3", now=t0 + 2.0 + i * (clamp_at / steps))
    assert d.deadline() == t0 + BATCH_MAX_WAIT, "整批上限钳制"
    assert d.batch_size == 3 + steps

    d.consume()
    assert d.batch_size == 0 and d.deadline() is None

    # 活跃开批：30s 内有过消息
    d2 = MessageDebouncer()
    d2.observe("a", now=t0)
    d2.consume()
    d2.observe("b", now=t0 + 10)
    assert d2.deadline() == t0 + 10 + OPEN_ACTIVE_WAIT, "活跃开批等待"
    print("debouncer OK")


def test_time_windows():
    print("time_windows:", rg.cfg.auto_reply.time_windows)

    # 类默认无时段表（运行配置可能覆盖，故直接构造断言默认值）
    from kanade_bot.plugins.chat.config import AutoReplyConfig

    assert AutoReplyConfig().time_windows == [], "默认无时段概率表"

    night = datetime(2026, 10, 26, 3, 0, tzinfo=TZ)
    noon = datetime(2026, 10, 26, 12, 0, tzinfo=TZ)

    # 窗口匹配能力：临时表验证跨零点、最先声明优先与时段外必送
    from datetime import time as dtime

    from kanade_bot.plugins.chat.config import TimeWindow

    evening = datetime(2026, 10, 26, 23, 30, tzinfo=TZ)
    saved = rg.cfg.auto_reply.time_windows
    rg.cfg.auto_reply.time_windows = []
    assert rg.window_probability(night) == (1.0, None), "空表：任何时段必送"
    assert rg.window_probability(noon) == (1.0, None)
    rg.cfg.auto_reply.time_windows = [
        TimeWindow(start=dtime(0, 0), end=dtime(7, 0), probability=0.0),
        TimeWindow(start=dtime(23, 0), end=dtime(7, 0), probability=0.3),
    ]
    try:
        assert rg.window_probability(night) == (0.0, rg.cfg.auto_reply.time_windows[0]), (
            "多窗口命中取最先声明者"
        )
        assert rg.window_probability(evening)[0] == 0.3, "跨零点：23:30 在 23:00-07:00"
        assert rg.window_probability(noon) == (1.0, None), "都不命中时概率 1.0"
    finally:
        rg.cfg.auto_reply.time_windows = saved

    meta = rg.GateMeta(
        new_messages=3,
        now=noon,
        since_bot_s=120.0,
        since_proactive_s=None,
        silence_streak=2,
    )
    prompt = rg.build_meta_prompt(meta)
    print("meta prompt:\n" + prompt)
    assert "最近 3 条" in prompt and "从未" in prompt
    print("time windows OK")


async def test_store_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        store = SessionStore(Path(td))
        # gate 表操作已从 SessionStore 独立为 GateStore（同库共享 registry）
        store = GateStore(store.registry)
        ids = await store.append_gate_messages(
            "s1",
            [
                ("user", "[10-26 14:02] 用户A(123)：大家好"),
                ("assistant", '{"reply": false, "reason": "灌水"}'),
            ],
        )
        assert ids == [1, 2], ids

        snap = await store.load_gate_history("s1")
        assert snap.rows == [
            (1, "user", "[10-26 14:02] 用户A(123)：大家好"),
            (2, "assistant", '{"reply": false, "reason": "灌水"}'),
        ]
        assert snap.total_rows == 2

        assert await store.get_gate_state("s1", "missing") is None

        # 清空决策历史：全量行删除，时间戳元信息 kv 保留
        await store.set_gate_state("s1", "last_bot_at", "1.5")
        assert await store.clear_gate_history("s1") == 2
        snap = await store.load_gate_history("s1")
        assert snap.rows == [] and snap.total_rows == 0
        assert await store.get_gate_state("s1", "last_bot_at") == "1.5"
        print("store roundtrip OK")


def test_plugin_load():
    import nonebot

    from kanade_bot.plugins.chat.agent.proactive.gate import (
        GATE_SYSTEM_PROMPT,
        _substitute_bot_name,
        gate_system_prompt,
    )
    from kanade_bot.plugins.chat.matcher import decision_clear, decision_stats  # noqa: F401

    nonebot.get_driver().config.nickname = "宵崎奏"
    prompt = gate_system_prompt()
    # 断言不依赖可编辑文案：与提示词文件（缺失/为空时内置回退）逐字节一致
    path = rg.cfg.auto_reply.gate.system_prompt_file_path
    source = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    expected = source if source else GATE_SYSTEM_PROMPT
    assert prompt == _substitute_bot_name(expected), "提示词文件（或内置回退）应生效"
    assert "{{ bot_name }}" not in prompt, "bot 名占位符应渲染（或文案未使用）"

    print("plugin load OK")


def test_auto_reply_notification():
    """决策理由注入系统通知：无理由仅场景说明，有理由附加在后"""
    from kanade_bot.plugins.chat.agent.proactive import (
        AUTO_REPLY_NOTIFICATION,
        build_auto_reply_notification,
    )

    assert build_auto_reply_notification(None) == AUTO_REPLY_NOTIFICATION
    assert build_auto_reply_notification("") == AUTO_REPLY_NOTIFICATION
    composed = build_auto_reply_notification("有人直接叫了名字")
    assert composed.startswith(AUTO_REPLY_NOTIFICATION)
    assert "有人直接叫了名字" in composed
    print("auto reply notification OK")


async def test_runtime_orchestration():
    """编排层端到端（mock 决策 LLM）：防抖到期 → 概率闸 → 决策 → 退避 → 回调重置"""
    from pydantic_ai import RunUsage

    from kanade_bot.plugins.chat.agent.proactive import ReplyDecision, auto_reply_runtime
    from kanade_bot.plugins.chat.agent.proactive.gate_history import (
        GateOutcome,
        SessionGateHistory,
    )
    from kanade_bot.utils.session import SessionInfo

    calls = {"n": 0}
    reply_flag = {"v": False}

    async def fake_decide(self, meta, *, timeout):
        calls["n"] += 1
        return GateOutcome(
            decision=ReplyDecision(reply=reply_flag["v"], reason="mocked"),
            usage=RunUsage(input_tokens=100, cache_read_tokens=50),
        )

    original_decide = SessionGateHistory.decide
    SessionGateHistory.decide = fake_decide

    class FakeBot:
        self_id = "smoke"

    cfg = rg.cfg.auto_reply
    sid = "smoke-runtime-test"
    info = SessionInfo(session_id=sid, platform="onebot", group_id="42", user_id="u1")
    try:
        cfg.enabled = True
        # 时段表临时清空：概率恒为 1.0，避免实盘时段表随运行时刻引入随机跳过
        saved_windows = cfg.time_windows
        cfg.time_windows = []
        # 启用群列表为运行时配置（chat_configs.json，按平台隔离）
        from kanade_bot.plugins.chat.config import chat_configs

        plat = chat_configs.instance.get_by_platform("onebot")
        saved_groups = plat.auto_reply_enabled_groups
        plat.auto_reply_enabled_groups = {"42"}
        start = cfg.backoff_start  # 退避起点（随配置，当前默认 2）

        # start 轮决策不回 → 计数到 start；再 1 轮验证到期改走退避延迟
        for round_no in range(1, start + 2):
            await auto_reply_runtime.on_group_message(
                sid,
                "42",
                f"[10-26 19:00] A(1): 第{round_no}条",
                author="1",
                bot=FakeBot(),
                session_info=info,
            )
            # 开批等待随防抖常量（首条安静/后续活跃），留 0.6s 余量；mock 决策秒回
            from kanade_bot.plugins.chat.agent.proactive.debounce import (
                OPEN_ACTIVE_WAIT,
                OPEN_QUIET_WAIT,
            )

            wait = OPEN_QUIET_WAIT if round_no == 1 else OPEN_ACTIVE_WAIT
            await asyncio.sleep(wait + 0.6)
            state = auto_reply_runtime._states[sid]
            if round_no <= start:
                assert calls["n"] == round_no, (round_no, calls["n"])
                assert state.backoff_count == round_no
                assert state.counters.silences == round_no
            else:
                # 计数到 start：到期改走退避延迟，不再调决策
                assert calls["n"] == start, calls["n"]
                assert state.backoff_timer is not None, "退避延迟任务应已安排"
                assert state.counters.skipped == 0

        state = auto_reply_runtime._states[sid]
        assert state.counters.input_tokens == 100 * start
        assert state.counters.cache_read_tokens == 50 * start

        # buffer 消费回调：取消挂起任务并重置防抖批次（退避计数保留）
        auto_reply_runtime.on_buffer_consumed(sid)
        assert state.timer is None
        assert state.debouncer.batch_size == 0
        assert state.backoff_count == start

        stats = await auto_reply_runtime.format_stats(sid, "42")
        print("stats:\n" + stats)
        assert f"连续沉默 {start}/{start}" in stats
        assert f"不回 {start}" in stats and "回 0" in stats

        # 「回」路径：清退避计数与挂起的重查任务后，决策 reply=True → 执行主动回复。
        # send_auto_reply 真实执行：buffer 空 → 前置检查跳过（覆盖 ..chat 延迟导入与执行链，
        # 不发起 LLM 调用与发送）
        if state.backoff_timer is not None:
            state.backoff_timer.cancel()
            state.backoff_timer = None
        state.backoff_count = 0
        reply_flag["v"] = True
        await auto_reply_runtime.on_group_message(
            sid,
            "42",
            "[10-26 19:01] B(2): kanade 在吗",
            author="2",
            bot=FakeBot(),
            session_info=info,
        )
        from kanade_bot.plugins.chat.agent.proactive.debounce import OPEN_ACTIVE_WAIT

        await asyncio.sleep(OPEN_ACTIVE_WAIT + 0.6)
        assert calls["n"] == start + 1
        assert state.counters.replies == 1
        assert state.backoff_count == 0, "决策回即清零"
        # buffer 空 → send_auto_reply 前置检查跳过：未实际发言，时间戳不更新
        assert state.last_proactive_at is None
        print("reply path OK")

        # 每日清空：调度任务已注册；内存生效序列归零，DB 全量保留
        from kanade_bot.plugins.chat.agent.proactive.runtime import scheduler as _scheduler

        job_ids = [str(job.id) for job in _scheduler.get_jobs()]
        assert any("_daily_reset" in job_id for job_id in job_ids), job_ids
        assert state.history.rows, "清空前内存应有生效行"
        await auto_reply_runtime._daily_reset()
        assert state.history.rows == []
        snap = await state.history.store.load_gate_history(sid)
        assert snap.total_rows > 0, "每日清空不删 DB 行"
        print("daily reset OK")

        # 清空决策上下文：DB 全量行删除、内存序列复位；未知会话返回 None
        assert await auto_reply_runtime.clear_history("no-such-session") is None
        deleted = await auto_reply_runtime.clear_history(sid)
        assert deleted == start + 2, deleted  # 每条群消息一行（mock 决策不 append）
        assert state.history.rows == []
        snap = await state.history.store.load_gate_history(sid)
        assert snap.rows == [] and snap.total_rows == 0
        assert state.counters.replies == 1, "统计计数不受清空影响"
        print("clear history OK")
        print("runtime orchestration OK")
    finally:
        state = auto_reply_runtime._states.pop(sid, None)
        for task in (state.timer, state.backoff_timer):
            if task is not None:
                task.cancel()
        SessionGateHistory.decide = original_decide
        cfg.enabled = False
        cfg.time_windows = saved_windows
        plat.auto_reply_enabled_groups = saved_groups
        # 清理测试会话库（_ensure_state 经 chat_manager.store 建的真实 SQLite）
        db_path = rg.cfg.session.sessions_dir_path / f"{sid}.sqlite3"
        for p in (
            db_path,
            db_path.with_name(db_path.name + "-wal"),
            db_path.with_name(db_path.name + "-shm"),
        ):
            p.unlink(missing_ok=True)


def test_set_group_enabled():
    """运行时启用/禁用切换：改写 chat_configs 并立即生效（save 落盘打桩）"""
    from kanade_bot.plugins.chat.agent.proactive import auto_reply_runtime, set_group_enabled
    from kanade_bot.plugins.chat.config import chat_configs

    cfg = rg.cfg.auto_reply
    plat = chat_configs.instance.get_by_platform("onebot")
    saved_groups = plat.auto_reply_enabled_groups
    # save 落盘打桩（ModelRegistryItem 为 pydantic 模型，需打桩类方法）
    saved_save = type(chat_configs).save_to_file
    type(chat_configs).save_to_file = lambda self: None
    try:
        cfg.enabled = False
        set_group_enabled("99", "onebot", True)
        assert not auto_reply_runtime.is_enabled("99", "onebot"), "总开关关闭时不生效"
        cfg.enabled = True
        assert auto_reply_runtime.is_enabled("99", "onebot"), "总开关+群启用"
        set_group_enabled("99", "onebot", False)
        assert not auto_reply_runtime.is_enabled("99", "onebot"), "群禁用后不生效"
        assert "99" not in plat.auto_reply_enabled_groups
    finally:
        type(chat_configs).save_to_file = saved_save
        cfg.enabled = False
        plat.auto_reply_enabled_groups = saved_groups
    print("set_group_enabled OK")


def main():
    test_debouncer()
    test_time_windows()
    asyncio.run(test_store_roundtrip())
    test_plugin_load()
    test_auto_reply_notification()
    test_set_group_enabled()
    asyncio.run(test_runtime_orchestration())
    print("SMOKE OK")


if __name__ == "__main__":
    main()
