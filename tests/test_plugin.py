import asyncio
import tempfile
from pathlib import Path
from typing import Any
import unittest

from store import SqliteGroupWakeStore
from filter import DynamicGroupWakeFilter
from main import GroupWakePlugin


class DummyMessage:
    def __init__(self, qq="12345"):
        self.qq = qq


class DummyMessageObj:
    def __init__(self, raw_message=None, sender=None):
        self.raw_message = raw_message
        self.sender = sender


class DummyRawWithRawData:
    def __init__(self, raw_data):
        self.raw_data = raw_data


class DummyEvent:
    def __init__(
        self,
        group_id: str = "",
        sender_id: str = "1001",
        message_str: str = "",
        messages: list | None = None,
        message_obj: Any = None,
        is_admin_val: bool = False,
    ):
        self._group_id = group_id
        self._sender_id = sender_id
        self.message_str = message_str
        self._messages = messages or []
        self.message_obj = message_obj or DummyMessageObj()
        self._is_admin = is_admin_val
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

    def is_admin(self):
        return self._is_admin


class DummyContext:
    def __init__(self):
        pass


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
        self.assertFalse(await self.store.add_wake_word("100", ""))  # Empty
        self.assertFalse(await self.store.add_wake_word("100", "   "))  # Whitespace

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

    def test_qq_official_member_role_management(self):
        plugin = GroupWakePlugin(DummyContext(), config={"allow_member_manage": False})
        plugin.store = self.store

        # 1. QQ 官方文档规范: author.member_role == 'admin' -> True
        msg_obj_admin = DummyMessageObj(raw_message=DummyRawWithRawData({"author": {"member_role": "admin"}}))
        event_admin = DummyEvent(group_id="100", message_obj=msg_obj_admin)
        self.assertTrue(plugin._can_manage(event_admin))

        # 2. QQ 官方文档规范: author.member_role == 'owner' -> True
        msg_obj_owner = DummyMessageObj(raw_message=DummyRawWithRawData({"author": {"member_role": "owner"}}))
        event_owner = DummyEvent(group_id="100", message_obj=msg_obj_owner)
        self.assertTrue(plugin._can_manage(event_owner))

        # 3. QQ 官方文档规范: author.member_role == 'member' -> False
        msg_obj_member = DummyMessageObj(raw_message=DummyRawWithRawData({"author": {"member_role": "member"}}))
        event_member = DummyEvent(group_id="100", message_obj=msg_obj_member)
        self.assertFalse(plugin._can_manage(event_member))

        # 4. AstrBot 全局管理员 -> True
        event_bot_admin = DummyEvent(group_id="100", is_admin_val=True)
        self.assertTrue(plugin._can_manage(event_bot_admin))

        # 5. 当开启 allow_member_manage 时，普通成员也可管理 -> True
        plugin_open = GroupWakePlugin(DummyContext(), config={"allow_member_manage": True})
        self.assertTrue(plugin_open._can_manage(event_member))


if __name__ == "__main__":
    unittest.main()
