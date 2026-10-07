"""定时任务模块单测：时间解析、JSON持久化、生命周期（创建/取消/过期跳过/重试）

参照 test_prompt_sections.py 的做法：桩掉 nonebot / apscheduler / crystal 等
重依赖后用 importlib 把 schedule.py 加载为 `kchat.agent.schedule`，
只测纯逻辑，不触碰真实调度器与模型运行。
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

REPO_ROOT = Path(__file__).parents[2]
SCHEDULE_PATH = REPO_ROOT / "kanade_bot/plugins/chat/agent/schedule.py"

TZ = types.SimpleNamespace()  # 占位，加载后替换为真实时区


class _LoggerStub:
    def info(self, *a, **k): ...
    def warning(self, *a, **k): ...
    def error(self, *a, **k): ...
    def exception(self, *a, **k): ...
    def debug(self, *a, **k): ...


class _DriverStub:
    def __init__(self):
        self.startup_hooks = []

    def on_startup(self, func):
        self.startup_hooks.append(func)

    def on_shutdown(self, func): ...


class _SchedulerStub:
    """记录 add_job / remove_job 调用的调度器桩"""

    def __init__(self):
        self.jobs: list[dict] = []
        self.removed: list[str] = []

    def add_job(self, func, trigger, **kwargs):
        self.jobs.append({"trigger": trigger, **kwargs})

    def remove_job(self, job_id):
        self.removed.append(job_id)
        raise KeyError(job_id)  # 模拟job不存在；模块内已捕获


_DRIVER = _DriverStub()
SCHEDULER_STUB = _SchedulerStub()


def _load_schedule_module():
    """把 schedule.py 加载成 kchat.agent.schedule，桩掉重依赖"""
    for name in (
        "kchat",
        "kchat.agent",
        "kanade_bot",
        "kanade_bot.utils",
        "kanade_bot.plugins",
    ):
        mod = types.ModuleType(name)
        mod.__path__ = []  # type: ignore[attr-defined]
        sys.modules[name] = mod

    nonebot_mod = types.ModuleType("nonebot")
    nonebot_mod.get_bot = lambda *a, **k: (_ for _ in ()).throw(KeyError("no bot"))
    nonebot_mod.get_driver = lambda: _DRIVER
    nonebot_mod.logger = _LoggerStub()
    nonebot_mod.require = lambda *a, **k: None
    sys.modules["nonebot"] = nonebot_mod

    ob_mod = types.ModuleType("nonebot.adapters.onebot.v11")
    ob_mod.Bot = type("Bot", (), {})
    sys.modules["nonebot.adapters.onebot.v11"] = ob_mod
    for pkg in ("nonebot.adapters",):
        m = types.ModuleType(pkg)
        m.__path__ = []
        sys.modules[pkg] = m

    aps_mod = types.ModuleType("nonebot_plugin_apscheduler")
    aps_mod.scheduler = SCHEDULER_STUB
    sys.modules["nonebot_plugin_apscheduler"] = aps_mod

    crystal_mod = types.ModuleType("kanade_bot.plugins.crystal")
    crystal_mod.get_crystal = lambda platform, user_id: 0
    crystal_mod.consume_crystal = lambda *a, **k: None
    sys.modules["kanade_bot.plugins.crystal"] = crystal_mod

    billing_mod = types.ModuleType("kanade_bot.utils.billing")
    billing_mod.compute_token_cost = lambda *a, **k: 1
    billing_mod.is_peak_hours = lambda *a, **k: False
    sys.modules["kanade_bot.utils.billing"] = billing_mod

    session_mod = types.ModuleType("kanade_bot.utils.session")

    class SessionInfo(BaseModel):
        """SessionInfo 最小替身（pydantic模型，字段兼容）"""

        session_id: str
        platform: str | None = None
        nickname: str | None = None
        user_id: str | None = None
        group_name: str | None = None
        group_id: str | None = None

    session_mod.SessionInfo = SessionInfo
    sys.modules["kanade_bot.utils.session"] = session_mod

    unused_store = Path(tempfile.gettempdir()) / "kanade_schedule_test_unused.json"
    cfg_module = types.ModuleType("kchat.config")

    class _ScheduledTaskCfg:
        data_file_path = unused_store
        retry_limit = 2
        retry_delay_minutes = 5

    cfg_module.cfg = types.SimpleNamespace(
        billing=types.SimpleNamespace(),
        scheduled_task=_ScheduledTaskCfg(),
    )
    sys.modules["kchat.config"] = cfg_module

    # _execute 内延迟导入的模块
    ban_mod = types.ModuleType("kchat.ban")
    ban_mod.is_banned = lambda *a, **k: False
    sys.modules["kchat.ban"] = ban_mod

    deliver_mod = types.ModuleType("kchat.deliver")

    async def _no_send(*a, **k): ...

    deliver_mod.send_onebot_proactive = _no_send
    deliver_mod.send_text_onebot_proactive = _no_send
    sys.modules["kchat.deliver"] = deliver_mod

    manager_mod = types.ModuleType("kchat.agent.manager")
    manager_mod.chat_manager = types.SimpleNamespace()
    sys.modules["kchat.agent.manager"] = manager_mod

    spec = importlib.util.spec_from_file_location("kchat.agent.schedule", SCHEDULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["kchat.agent.schedule"] = module
    spec.loader.exec_module(module)
    return module


_schedule = _load_schedule_module()

parse_run_at = _schedule.parse_run_at
ScheduledTask = _schedule.ScheduledTask
ScheduledTaskStore = _schedule.ScheduledTaskStore
ScheduledTaskManager = _schedule.ScheduledTaskManager
TZ = _schedule.TZ


def _make_task(**overrides) -> ScheduledTask:
    """构造一个最小合法任务，字段可覆盖"""
    now = datetime.now(TZ)
    fields = {
        "task_id": "abc123",
        "session_id": "qq-group-1",
        "session_info": {
            "session_id": "qq-group-1",
            "platform": "onebot",
            "group_id": "1",
        },
        "bot_id": "bot1",
        "description": "测试任务",
        "creator_id": "42",
        "creator_name": "测试者",
        "created_at": now.isoformat(),
        "run_at": (now + timedelta(hours=1)).isoformat(),
    }
    fields.update(overrides)
    return ScheduledTask.model_validate(fields)


def _make_manager(tmpdir: Path, *, retry_limit: int = 2, retry_delay_minutes: int = 5):
    store = ScheduledTaskStore(tmpdir / "tasks.json")
    manager = ScheduledTaskManager(
        store, retry_limit=retry_limit, retry_delay_minutes=retry_delay_minutes
    )
    return manager, store


class TestParseRunAt(unittest.TestCase):
    """时间参数解析"""

    def test_absolute_future(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
        dt = parse_run_at("2026-10-08T08:00", 0, now=now)
        self.assertEqual(dt, datetime(2026, 10, 8, 8, 0, tzinfo=TZ))

    def test_absolute_with_space_separator(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
        dt = parse_run_at("2026-10-08 08:00:30", 0, now=now)
        self.assertEqual(dt, datetime(2026, 10, 8, 8, 0, 30, tzinfo=TZ))

    def test_absolute_aware_converted(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
        dt = parse_run_at("2026-10-08T00:00:00+00:00", 0, now=now)
        self.assertEqual(dt.tzinfo, TZ)
        self.assertEqual(dt, datetime(2026, 10, 8, 8, 0, tzinfo=TZ))

    def test_delay(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
        self.assertEqual(parse_run_at("", 30, now=now), now + timedelta(minutes=30))
        self.assertEqual(parse_run_at("", 0.5, now=now), now + timedelta(seconds=30))

    def test_both_provided_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_at("2026-10-08T08:00", 10)

    def test_neither_provided_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_at("", 0)

    def test_negative_delay_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_at("", -5)

    def test_past_time_rejected(self):
        now = datetime(2026, 10, 7, 12, 0, tzinfo=TZ)
        with self.assertRaises(ValueError):
            parse_run_at("2026-10-07T08:00", 0, now=now)

    def test_invalid_format_rejected(self):
        with self.assertRaises(ValueError):
            parse_run_at("明天早上八点", 0, now=datetime.now(TZ))


class TestStore(unittest.TestCase):
    """JSON持久化往返与损坏文件兜底"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_round_trip(self):
        store = ScheduledTaskStore(self.tmp / "tasks.json")
        task = _make_task()
        store.save([task])
        loaded = store.load()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].task_id, task.task_id)
        self.assertEqual(loaded[0].description, task.description)
        self.assertEqual(loaded[0].run_at_dt, task.run_at_dt)

    def test_missing_file(self):
        store = ScheduledTaskStore(self.tmp / "absent.json")
        self.assertEqual(store.load(), [])

    def test_corrupted_file(self):
        path = self.tmp / "tasks.json"
        path.write_text("{ not json", encoding="utf-8")
        store = ScheduledTaskStore(path)
        self.assertEqual(store.load(), [])


class TestManagerLifecycle(unittest.IsolatedAsyncioTestCase):
    """创建/取消/启动恢复/触发重试"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    async def test_create_persists_and_schedules(self):
        manager, store = _make_manager(self.tmp)
        from kanade_bot.utils.session import SessionInfo  # 桩

        info = {
            "session_id": "qq-group-1",
            "platform": "onebot",
            "group_id": "1",
            "user_id": "42",
        }
        task = await manager.create(
            session_info=SessionInfo(**info),
            bot_id="bot1",
            description="喝水提醒",
            run_at=datetime.now(TZ) + timedelta(minutes=10),
        )
        self.assertTrue(task.task_id)
        # 已持久化
        loaded = store.load()
        self.assertEqual([t.task_id for t in loaded], [task.task_id])
        # 已注册调度job
        self.assertTrue(any(j["id"].endswith(task.task_id) for j in SCHEDULER_STUB.jobs))
        # 列表按会话过滤
        self.assertEqual(len(manager.list_by_session("qq-group-1")), 1)
        self.assertEqual(manager.list_by_session("qq-private-9"), [])

    async def test_cancel_scoped_by_session(self):
        manager, store = _make_manager(self.tmp)
        task = _make_task()
        async with manager._lock:
            manager._tasks[task.task_id] = task
        # 其他会话不能取消
        self.assertFalse(await manager.cancel(task.task_id, session_id="qq-private-9"))
        # 所属会话可以取消
        self.assertTrue(await manager.cancel(task.task_id, session_id=task.session_id))
        self.assertEqual(store.load(), [])
        self.assertFalse(await manager.cancel("nonexist"))

    async def test_startup_skips_expired(self):
        manager, store = _make_manager(self.tmp)
        now = datetime.now(TZ)
        expired = _make_task(task_id="dead01", run_at=(now - timedelta(minutes=1)).isoformat())
        alive = _make_task(task_id="alive1", run_at=(now + timedelta(hours=1)).isoformat())
        store.save([expired, alive])

        await manager.startup()
        self.assertEqual([t.task_id for t in manager.list_by_session("qq-group-1")], ["alive1"])
        # 过期任务已被从文件清理
        persisted = store.load()
        self.assertEqual([t.task_id for t in persisted], ["alive1"])

    async def test_fire_success_removes_task(self):
        manager, store = _make_manager(self.tmp)
        task = _make_task()
        async with manager._lock:
            manager._tasks[task.task_id] = task

        async def _ok(t):
            return None

        manager._execute = _ok
        await manager._fire(task.task_id)
        self.assertEqual(store.load(), [])
        self.assertEqual(manager.list_by_session(task.session_id), [])

    async def test_fire_retries_then_drops(self):
        manager, store = _make_manager(self.tmp, retry_limit=1, retry_delay_minutes=5)
        task = _make_task()
        async with manager._lock:
            manager._tasks[task.task_id] = task

        calls = []

        async def _fail(t):
            calls.append(t.task_id)
            raise RuntimeError("boom")

        manager._execute = _fail

        await manager._fire(task.task_id)  # 首次失败 → 重试
        self.assertEqual(len(calls), 1)
        self.assertEqual(store.load()[0].attempts, 1)
        # run_at 已更新为重试时间（未来）
        self.assertGreater(store.load()[0].run_at_dt, datetime.now(TZ))

        await manager._fire(task.task_id)  # 重试仍失败 → 耗尽丢弃
        self.assertEqual(len(calls), 2)
        self.assertEqual(store.load(), [])
        self.assertEqual(manager.list_by_session(task.session_id), [])

    async def test_fire_unknown_task_noop(self):
        manager, _store = _make_manager(self.tmp)
        await manager._fire("ghost0")  # 不应抛错


if __name__ == "__main__":
    unittest.main()
