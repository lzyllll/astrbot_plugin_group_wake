import asyncio
import tempfile
from pathlib import Path
import unittest

from store import SqliteGroupWakeStore
from filter import DynamicGroupWakeFilter


class DummyMessage:
    def __init__(self, qq="12345"):
        self.qq = qq


class DummyEvent:
    def __init__(self, group_id: str = "", sender_id: str = "1001", message_str: str = "", messages: list | None = None):
        self._group_id = group_id
        self._sender_id = sender_id
        self.message_str = message_str
        self._messages = messages or []
        self.is_wake = False
        self.is_at_or_wake_command = False

    def get_group_id(self):
        return self._group_id

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return "999999"

    def get_messages(self):
        return self._messages


class TestGroupWake(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.store = SqliteGroupWakeStore(self.db_path)
        await self.store.init()
        DynamicGroupWakeFilter.set_store(self.store)
        self.filter = DynamicGroupWakeFilter()

    async def asyncTearDown(self):
        self.temp_dir.cleanup()

    async def test_store_crud(self):
        # 1. Add words
        self.assertTrue(await self.store.add_wake_word("100", "小助手"))
        self.assertTrue(await self.store.add_wake_word("100", "管家"))
        self.assertFalse(await self.store.add_wake_word("100", "小助手"))  # Duplicate

        # 2. Get and count
        self.assertEqual(self.store.count_wake_words("100"), 2)
        words = self.store.list_wake_words("100")
        self.assertEqual(words, ["小助手", "管家"])

        # 3. Isolation between groups
        self.assertEqual(self.store.get_wake_words("200"), set())

        # 4. Remove word
        self.assertTrue(await self.store.remove_wake_word("100", "管家"))
        self.assertFalse(await self.store.remove_wake_word("100", "管家"))
        self.assertEqual(self.store.list_wake_words("100"), ["小助手"])

        # 5. Clear group
        self.assertTrue(await self.store.add_wake_word("100", "Bot"))
        count = await self.store.clear_group("100")
        self.assertEqual(count, 2)
        self.assertEqual(self.store.get_wake_words("100"), set())

    async def test_filter_matching(self):
        await self.store.add_wake_word("100", "小助手")
        await self.store.add_wake_word("100", "小助")

        # 1. Private chat must be rejected
        event_private = DummyEvent(group_id="", message_str="小助手 你好")
        self.assertFalse(self.filter.filter(event_private))

        # 2. Non-matching message
        event_unmatched = DummyEvent(group_id="100", message_str="今天天气很好")
        self.assertFalse(self.filter.filter(event_unmatched))

        # 3. Greedy matching: "小助手" should be matched over "小助"
        event_match = DummyEvent(group_id="100", message_str="小助手 查战力")
        self.assertTrue(self.filter.filter(event_match))
        self.assertEqual(event_match.message_str, "查战力")
        self.assertTrue(event_match.is_wake)
        self.assertTrue(event_match.is_at_or_wake_command)

        # 4. Alone wake word
        event_alone = DummyEvent(group_id="100", message_str="小助手")
        self.assertTrue(self.filter.filter(event_alone))
        self.assertEqual(event_alone.message_str, "")
        self.assertTrue(event_alone.is_wake)
        self.assertTrue(event_alone.is_at_or_wake_command)


if __name__ == "__main__":
    unittest.main()
