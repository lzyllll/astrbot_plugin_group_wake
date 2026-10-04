from __future__ import annotations

from typing import Any

from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import At
from astrbot.core.star.filter.custom_filter import CustomFilter

try:
    from .store import SqliteGroupWakeStore
except ImportError:
    from store import SqliteGroupWakeStore


class DynamicGroupWakeFilter(CustomFilter):
    """分群专属动态唤醒词前置过滤器。

    时序逻辑：
    1. 仅在群聊中生效，私聊直接拦截返回 False，避免污染私聊原生逻辑；
    2. 极速读缓存：通过内存哈希表检查本群是否配置了专属唤醒词；
    3. 消息段冲突防护：首个段为 @ 他人时不抢占唤醒；
    4. 贪婪匹配：优先匹配较长的唤醒词（避免短词前缀截断长词）；
    5. 就地切分：剥离唤醒词前缀并更新 event.message_str；
    6. 点亮状态：激活 event.is_wake 与 event.is_at_or_wake_command。
    """

    store: SqliteGroupWakeStore | None = None

    @classmethod
    def set_store(cls, store: SqliteGroupWakeStore) -> None:
        cls.store = store

    def filter(self, event: AstrMessageEvent, cfg: Any = None) -> bool:
        if self.store is None:
            return False

        # 1. 严格守卫：仅群聊生效
        group_id = str(event.get_group_id() or "")
        if not group_id:
            return False

        # 2. 检查本群是否有配置专属唤醒词
        wake_words = self.store.get_wake_words(group_id)
        if not wake_words:
            return False

        # 3. 消息段冲突防护：若以 At 他人开头（非 At 机器人，非 At 全体成员），不唤醒
        try:
            messages = event.get_messages()
            if (
                messages
                and isinstance(messages[0], At)
                and str(messages[0].qq) != str(event.get_self_id())
                and str(messages[0].qq) != "all"
            ):
                return False
        except Exception:
            pass

        # 4. 检查当前消息文本
        msg = event.message_str.strip() if event.message_str else ""
        if not msg:
            return False

        # 5. 最长前缀贪婪匹配
        matched_wake = None
        for word in sorted(wake_words, key=len, reverse=True):
            if msg.startswith(word):
                matched_wake = word
                break

        if not matched_wake:
            return False

        # 6. 成功命中：剥离唤醒词前缀并保存至 event.message_str
        clean_msg = msg[len(matched_wake) :].strip()
        event.message_str = clean_msg

        # 7. 点亮唤醒标志与后续所有普通指令准入开关
        event.is_wake = True
        event.is_at_or_wake_command = True

        return True
