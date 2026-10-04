from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from .filter import DynamicGroupWakeFilter
    from .store import SqliteGroupWakeStore
except ImportError:
    from filter import DynamicGroupWakeFilter
    from store import SqliteGroupWakeStore

_PLUGIN_DIR = Path(__file__).resolve().parent


@register(
    "astrbot_plugin_group_wake",
    "lzy",
    "AstrBot 分群专属动态唤醒词插件，支持群唤醒词动态增删、SQLite持久化与零I/O延迟缓存",
    "1.0.0",
    "https://github.com/lzyllll/astrbot_plugin_group_wake",
)
class GroupWakePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config = config

        data_dir = _PLUGIN_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = SqliteGroupWakeStore(data_dir / "group_wake.db")

        # 关联 Filter 与存储实例
        DynamicGroupWakeFilter.set_store(self.store)

    async def initialize(self) -> None:
        """插件激活时初始化 SQLite 数据库并预热内存读缓存。"""
        await self.store.init()
        logger.info("[astrbot_plugin_group_wake] 插件初始化完成，SQLite 缓存已预热")

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        """Bot 启动钩子：确保开机全量预热。"""
        await self.store.init()

    # --------------------------------------------------------------------------
    # 配置辅助方法
    # --------------------------------------------------------------------------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        if isinstance(self.config, dict):
            return bool(self.config.get(key, default))
        if hasattr(self.config, "get"):
            return bool(self.config.get(key, default))
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        val = None
        if isinstance(self.config, dict):
            val = self.config.get(key)
        elif hasattr(self.config, "get"):
            val = self.config.get(key)
        if val is not None:
            try:
                return int(val)
            except (ValueError, TypeError):
                pass
        return default

    def _cfg_str(self, key: str, default: str) -> str:
        if isinstance(self.config, dict):
            return str(self.config.get(key, default))
        if hasattr(self.config, "get"):
            return str(self.config.get(key, default))
        return default

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        """鉴权：检查当前发送者是否有权管理本群唤醒词。

        依据 QQ 机器人官方文档 (https://bot.q.qq.com/wiki/develop/api-v2/autogen/event/c2c_message_create.html)：
        author.member_role:
          - "admin": 管理员
          - "owner": 群主
          - "member": 普通成员
        """
        # 1. 配置项：若开启允许普通成员管理
        if self._cfg_bool("allow_member_manage", False):
            return True

        # 2. AstrBot 全局管理员配置 (admins_id)
        if event.is_admin():
            return True

        # 3. 根据 QQ 官方机器人标准文档结构解析 author.member_role
        raw_msg = getattr(event.message_obj, "raw_message", None)
        if raw_msg is not None:
            # 3.1 qqofficial 适配器中的 PatchedGroupMessage.raw_data
            raw_data = getattr(raw_msg, "raw_data", None)
            if isinstance(raw_data, dict):
                author = raw_data.get("author")
                if isinstance(author, dict):
                    member_role = str(author.get("member_role") or "").lower()
                    if member_role in ("admin", "owner"):
                        return True

            # 3.2 raw_msg 本身为字典（Webhook 或 OneBot）
            if isinstance(raw_msg, dict):
                # QQ 官方结构: author.member_role
                author = raw_msg.get("author")
                if isinstance(author, dict):
                    member_role = str(author.get("member_role") or "").lower()
                    if member_role in ("admin", "owner"):
                        return True
                # OneBot/aiocqhttp 结构: sender.role
                sender = raw_msg.get("sender")
                if isinstance(sender, dict):
                    sender_role = str(sender.get("role") or "").lower()
                    if sender_role in ("admin", "owner"):
                        return True

            # 3.3 raw_msg.author 对象属性
            author_obj = getattr(raw_msg, "author", None)
            if author_obj is not None:
                if isinstance(author_obj, dict):
                    member_role = str(author_obj.get("member_role") or "").lower()
                else:
                    member_role = str(getattr(author_obj, "member_role", None) or "").lower()
                if member_role in ("admin", "owner"):
                    return True

        # 4. 检查 AstrBot 标准化 sender 字段
        sender = getattr(event.message_obj, "sender", None)
        if sender is not None:
            role = str(getattr(sender, "member_role", None) or getattr(sender, "role", None) or "").lower()
            if role in ("admin", "owner"):
                return True
            if hasattr(sender, "__dict__"):
                d_role = str(sender.__dict__.get("member_role") or sender.__dict__.get("role") or "").lower()
                if d_role in ("admin", "owner"):
                    return True

        return False

    # --------------------------------------------------------------------------
    # 核心唤醒前置处理器（最高优先级 10000）
    # --------------------------------------------------------------------------
    @filter.custom_filter(DynamicGroupWakeFilter, priority=10000)
    async def dynamic_wake_entry(self, event: AstrMessageEvent):
        """动态唤醒前置放行节点。

        当群消息命中专属唤醒词后：
        1. 若用户仅发送了唤醒词本身，根据配置发送友好问候并拦截后续流转；
        2. 若后面跟随了指令或自然语言，此处理器不产生拦截，消息自动放行给后续普通指令与 LLM。
        """
        if not event.message_str:
            if self._cfg_bool("enable_reply_on_wake_only", True):
                reply_text = self._cfg_str("reply_on_wake_only_text", "在呢！有什么可以帮您的？")
                yield event.plain_result(reply_text)
                event.stop_event()

    # --------------------------------------------------------------------------
    # 管理指令
    # --------------------------------------------------------------------------
    @filter.command("设置本群唤醒词")
    async def set_group_wake(self, event: AstrMessageEvent, word: str = ""):
        """为当前群添加专属动态唤醒词。"""
        async for res in self._do_add_wake(event, word):
            yield res

    @filter.command("添加本群唤醒词")
    async def add_group_wake(self, event: AstrMessageEvent, word: str = ""):
        """别名：为当前群添加专属动态唤醒词。"""
        async for res in self._do_add_wake(event, word):
            yield res

    async def _do_add_wake(self, event: AstrMessageEvent, word: str):
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用。")
            return

        if not self._can_manage(event):
            yield event.plain_result("❌ 权限不足：仅本群管理员或机器人管理员可配置本群唤醒词。")
            return

        clean_word = word.strip()
        if not clean_word:
            yield event.plain_result(
                "💡 请提供要设置的唤醒词。\n"
                "用法示例：/设置本群唤醒词 小助手\n\n"
                "⚠️ 唤醒词不能为空字符。若需要清空本群所有唤醒词，可发送「/清空本群唤醒词」。"
            )
            return

        if len(clean_word) > 20:
            yield event.plain_result("❌ 唤醒词长度不能超过 20 个字符。")
            return

        max_count = self._cfg_int("max_wake_words_per_group", 10)
        current_count = self.store.count_wake_words(group_id)
        if current_count >= max_count:
            yield event.plain_result(
                f"❌ 本群唤醒词数量已达上限（{max_count} 个）。\n"
                "请先使用「/删除本群唤醒词 <词>」移除不需要的唤醒词。"
            )
            return

        added = await self.store.add_wake_word(group_id, clean_word)
        if added:
            yield event.plain_result(
                f"🎉 已成功为本群添加专属唤醒词：「{clean_word}」！\n"
                f"💡 现在在群内发送「{clean_word} + 内容」即可免 @ 触发机器人。"
            )
        else:
            yield event.plain_result(f"💡 唤醒词「{clean_word}」已存在于本群列表中。")

    @filter.command("删除本群唤醒词")
    async def remove_group_wake(self, event: AstrMessageEvent, word: str = ""):
        """从当前群移除指定动态唤醒词。"""
        async for res in self._do_remove_wake(event, word):
            yield res

    @filter.command("移除本群唤醒词")
    async def delete_group_wake(self, event: AstrMessageEvent, word: str = ""):
        """别名：从当前群移除指定动态唤醒词。"""
        async for res in self._do_remove_wake(event, word):
            yield res

    async def _do_remove_wake(self, event: AstrMessageEvent, word: str):
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用。")
            return

        if not self._can_manage(event):
            yield event.plain_result("❌ 权限不足：仅本群管理员或机器人管理员可配置本群唤醒词。")
            return

        clean_word = word.strip()
        if not clean_word:
            yield event.plain_result("💡 请提供要删除的唤醒词。\n例如：/删除本群唤醒词 小助手")
            return

        removed = await self.store.remove_wake_word(group_id, clean_word)
        if removed:
            yield event.plain_result(f"✅ 已成功移除本群唤醒词：「{clean_word}」。")
        else:
            yield event.plain_result(f"💡 本群未配置唤醒词：「{clean_word}」。")

    @filter.command("查看本群唤醒词")
    async def list_group_wake(self, event: AstrMessageEvent):
        """查看当前群生效的专属动态唤醒词。"""
        async for res in self._do_list_wake(event):
            yield res

    @filter.command("本群唤醒词")
    async def show_group_wake(self, event: AstrMessageEvent):
        """别名：查看当前群生效的专属动态唤醒词。"""
        async for res in self._do_list_wake(event):
            yield res

    async def _do_list_wake(self, event: AstrMessageEvent):
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用。")
            return

        wake_words = self.store.list_wake_words(group_id)
        if not wake_words:
            yield event.plain_result(
                "📋 本群当前暂未配置专属唤醒词（唤醒词列表为空）。\n"
                "💡 此时机器人仅在被 @ 或使用全局前缀（如 /）时响应。\n"
                "💡 群管理员可发送「/设置本群唤醒词 <词>」为本群添加专属免 @ 唤醒词。"
            )
            return

        lines = ["📋 本群专属唤醒词列表："]
        for idx, w in enumerate(wake_words, 1):
            lines.append(f"{idx}. 「{w}」")
        lines.append("\n💡 提示：在群内发送「唤醒词 + 内容」即可免 @ 触发机器人！")
        yield event.plain_result("\n".join(lines))

    @filter.command("清空本群唤醒词")
    async def clear_group_wake(self, event: AstrMessageEvent):
        """清空当前群的所有专属动态唤醒词（限管理员）。"""
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield event.plain_result("❌ 该指令仅支持在群聊中使用。")
            return

        if not self._can_manage(event):
            yield event.plain_result("❌ 权限不足：仅本群管理员或机器人管理员可清空本群唤醒词。")
            return

        count = await self.store.clear_group(group_id)
        if count > 0:
            yield event.plain_result(f"✅ 已成功清空本群所有专属唤醒词（共移除 {count} 个）。")
        else:
            yield event.plain_result("💡 本群当前暂无配置任何专属唤醒词。")

    @filter.command("唤醒词 帮助")
    async def group_wake_space_help(self, event: AstrMessageEvent):
        """查看分群动态唤醒词帮助说明。"""
        async for res in self._do_help(event):
            yield res

    @filter.command("唤醒词")
    async def group_wake_short_help(self, event: AstrMessageEvent):
        """别名：查看分群动态唤醒词帮助说明。"""
        async for res in self._do_help(event):
            yield res

    @filter.command("唤醒词帮助")
    async def group_wake_help(self, event: AstrMessageEvent):
        """别名：查看分群动态唤醒词帮助说明。"""
        async for res in self._do_help(event):
            yield res

    async def _do_help(self, event: AstrMessageEvent):
        help_text = (
            "📖【分群动态唤醒词使用指南】\n\n"
            "• /设置本群唤醒词 <词>：为本群添加专属免 @ 唤醒词\n"
            "• /删除本群唤醒词 <词>：移除本群已配置的唤醒词\n"
            "• /查看本群唤醒词：查看当前群所有生效的唤醒词\n"
            "• /清空本群唤醒词：清空当前群所有专属唤醒词（限管理员）\n"
            "• /唤醒词 帮助：查看本使用指南\n\n"
            "💡 特性亮点：\n"
            "1. 仅当前群生效：私聊与不同群之间严格隔离；\n"
            "2. 零 I/O 极速响应：基于内存读缓存，消息匹配微秒级延迟；\n"
            "3. SQLite 持久化：唤醒词跨重启永久保存。"
        )
        yield event.plain_result(help_text)
