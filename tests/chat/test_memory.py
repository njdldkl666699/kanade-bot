import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import nonebot
from copilot.tools import ToolInvocation

nonebot.init(_env_file=None, log_level="WARNING")

from kanade_bot.utils.session import SessionInfo

MEMORY_MODULE_PATH = Path(__file__).parents[2] / "kanade_bot/plugins/chat/agent/memory.py"
MEMORY_SPEC = importlib.util.spec_from_file_location("chat_memory_under_test", MEMORY_MODULE_PATH)
assert MEMORY_SPEC is not None and MEMORY_SPEC.loader is not None
MEMORY_MODULE = importlib.util.module_from_spec(MEMORY_SPEC)
sys.modules[MEMORY_SPEC.name] = MEMORY_MODULE
MEMORY_SPEC.loader.exec_module(MEMORY_MODULE)

MemoryScope = MEMORY_MODULE.MemoryScope
MemoryContext = MEMORY_MODULE.MemoryContext
MemoryStore = MEMORY_MODULE.MemoryStore
build_memory_tools = MEMORY_MODULE.build_memory_tools


class MemoryStoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")
        self.user = MemoryScope("onebot", "user", "10001")
        self.other_user = MemoryScope("onebot", "user", "10002")
        self.group = MemoryScope("onebot", "group", "20001")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_save_updates_same_topic_in_place(self):
        original = await self.store.save(self.user, "music_preference", "用户喜欢摇滚乐。")
        updated = await self.store.save(self.user, "music_preference", "用户喜欢安静的音乐。")

        self.assertEqual(original.id, updated.id)
        records = await self.store.search([self.user], "音乐", 10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].content, "用户喜欢安静的音乐。")

    async def test_searches_chinese_terms_and_isolates_scopes(self):
        await self.store.save(self.user, "music_preference", "用户喜欢安静的音乐。")
        await self.store.save(self.other_user, "music_preference", "用户喜欢重金属音乐。")
        await self.store.save(self.group, "meeting", "群聊约定每周六晚上开会。")

        records = await self.store.search([self.user, self.group], "喜欢什么音乐", 10)
        self.assertEqual([record.content for record in records], ["用户喜欢安静的音乐。"])

        recent = await self.store.search([self.group], "", 10)
        self.assertEqual([record.topic for record in recent], ["meeting"])

    async def test_delete_requires_matching_scope(self):
        record = await self.store.save(self.user, "nickname", "用户希望被称为小可。")

        self.assertFalse(await self.store.delete(self.other_user, record.id))
        self.assertEqual(len(await self.store.search([self.user], "", 10)), 1)
        self.assertTrue(await self.store.delete(self.user, record.id))
        self.assertEqual(await self.store.search([self.user], "", 10), [])

    async def test_capacity_evicts_oldest_record(self):
        store = MemoryStore(
            Path(self.temp_dir.name) / "limited.sqlite3",
            max_memories_per_scope=2,
        )
        await store.save(self.user, "first", "第一条")
        await store.save(self.user, "second", "第二条")
        await store.save(self.user, "third", "第三条")

        records = await store.search([self.user], "", 10)
        self.assertEqual({record.topic for record in records}, {"second", "third"})

    async def test_imports_legacy_markdown_once(self):
        legacy_dir = Path(self.temp_dir.name) / "users"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "onebot-10001.md"
        legacy_file.write_text("用户以前说过喜欢深夜作曲。\n", encoding="utf-8")
        store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")

        records = await store.search([self.user], "深夜作曲", 10)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].topic, "legacy_markdown_import")

        legacy_file.write_text("这次修改不应被重复导入。\n", encoding="utf-8")
        reloaded_store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")
        records = await reloaded_store.search([self.user], "", 10)
        self.assertEqual(records[0].content, "用户以前说过喜欢深夜作曲。")


class MemoryToolsTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.temp_dir.name) / "memory.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def test_tools_are_bound_to_current_user_and_group(self):
        info = SessionInfo(
            session_id="qq-group-20001",
            platform="onebot",
            user_id="10001",
            group_id="20001",
        )
        context = MemoryContext.from_session_info(info)
        tools = {tool.name: tool for tool in build_memory_tools(context, self.store)}

        save_result = await tools["save_memory"].handler(
            ToolInvocation(
                arguments={
                    "scope": "user",
                    "topic": "favorite_color",
                    "content": "用户喜欢绿色。",
                }
            )
        )
        self.assertEqual(save_result.result_type, "success")

        recall_result = await tools["recall_memory"].handler(
            ToolInvocation(arguments={"query": "绿色", "scopes": ["user", "group"]})
        )
        self.assertIn("用户喜欢绿色", recall_result.text_result_for_llm)
        self.assertIn("scope=user", recall_result.text_result_for_llm)

        context.update(info.model_copy(update={"user_id": "10002"}))
        other_user_result = await tools["recall_memory"].handler(
            ToolInvocation(arguments={"query": "", "scopes": ["user"]})
        )
        self.assertEqual(other_user_result.text_result_for_llm, "没有找到相关记忆。")

        context.update(info)
        original_user_result = await tools["recall_memory"].handler(
            ToolInvocation(arguments={"query": "", "scopes": ["user"]})
        )
        self.assertIn("用户喜欢绿色", original_user_result.text_result_for_llm)

    async def test_private_session_cannot_write_group_memory(self):
        info = SessionInfo(
            session_id="qq-private-10001",
            platform="onebot",
            user_id="10001",
        )
        context = MemoryContext.from_session_info(info)
        tools = {tool.name: tool for tool in build_memory_tools(context, self.store)}

        result = await tools["save_memory"].handler(
            ToolInvocation(
                arguments={
                    "scope": "group",
                    "topic": "rule",
                    "content": "群聊规则。",
                }
            )
        )
        self.assertIn("没有可用的 group", result.text_result_for_llm)


if __name__ == "__main__":
    unittest.main()
