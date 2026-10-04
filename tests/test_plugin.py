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


class DummyResult:
    def __init__(self, text: str = ""):
        self.text = text
        self.is_markdown = False

    def message(self, text: str):
        self.text = text
        return self

    def use_markdown(self, val: bool):
        self.is_markdown = val
        return self


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

    def plain_result(self, text: str):
        return DummyResult(text)

    def make_result(self):
        return DummyResult()


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

    async def test_markdown_tips_and_interactive_tags(self):
        plugin = GroupWakePlugin(DummyContext(), config={"allow_member_manage": False})
        plugin.store = self.store

        event = DummyEvent(group_id="100")
        results = [res async for res in plugin._do_help(event)]
        self.assertEqual(len(results), 1)
        res = results[0]

        # 1. 验证 Markdown 与 Tips 格式
        self.assertTrue(res.is_markdown)
        self.assertIn("> 💡 **分群动态唤醒词使用指南**", res.text)

        # 2. 验证点击交互标签 <qqbot-cmd-input>
        self.assertIn("<qqbot-cmd-input", res.text)
        self.assertIn('show="设置本群唤醒词"', res.text)
        self.assertIn('show="删除本群唤醒词"', res.text)
        self.assertIn('show="查看本群唤醒词"', res.text)
        self.assertIn('show="帮助 唤醒词"', res.text)

        # 3. 验证群主开启接收全部消息的提示
        self.assertIn("接收所有消息", res.text)
        self.assertIn("只能接收到 @消息", res.text)

    async def test_command_operations_markdown(self):
        plugin = GroupWakePlugin(DummyContext(), config={"allow_member_manage": False})
        plugin.store = self.store

        # 管理员事件
        admin_event = DummyEvent(
            group_id="100",
            message_obj=DummyMessageObj(raw_message=DummyRawWithRawData({"author": {"member_role": "admin"}})),
        )

        # 1. 添加唤醒词成功
        results = [res async for res in plugin._do_add_wake(admin_event, "小精灵")]
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_markdown)
        self.assertIn("> 🎉 **唤醒词添加成功**", results[0].text)
        self.assertIn("小精灵", results[0].text)
        self.assertIn("接收所有消息", results[0].text)

        # 2. 查看唤醒词
        list_res = [res async for res in plugin._do_list_wake(admin_event)]
        self.assertEqual(len(list_res), 1)
        self.assertTrue(list_res[0].is_markdown)
        self.assertIn("> 💡 **本群专属唤醒词列表**", list_res[0].text)
        self.assertIn("小精灵", list_res[0].text)
        self.assertIn("<qqbot-cmd-input", list_res[0].text)  # 包含快捷删除标签

        # 3. 删除唤醒词
        del_res = [res async for res in plugin._do_remove_wake(admin_event, "小精灵")]
        self.assertEqual(len(del_res), 1)
        self.assertTrue(del_res[0].is_markdown)
        self.assertIn("> 🎉 **唤醒词移除成功**", del_res[0].text)

        # 4. 普通成员无权操作提示
        member_event = DummyEvent(
            group_id="100",
            message_obj=DummyMessageObj(raw_message=DummyRawWithRawData({"author": {"member_role": "member"}})),
        )
        unauth_res = [res async for res in plugin._do_add_wake(member_event, "小精灵")]
        self.assertEqual(len(unauth_res), 1)
        self.assertTrue(unauth_res[0].is_markdown)
        self.assertIn("> ⚠️ **权限不足**", unauth_res[0].text)


if __name__ == "__main__":
    unittest.main()

