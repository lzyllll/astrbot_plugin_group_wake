from __future__ import annotations

import html
from pathlib import Path
from typing import Any
import urllib.parse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register

try:
    from .filter import DynamicGroupWakeFilter
    from .store import SqliteGroupWakeStore
except ImportError:
    from filter import DynamicGroupWakeFilter
    from store import SqliteGroupWakeStore

_PLUGIN_DIR = Path(__file__).resolve().parent


def safe_quote_cmd(cmd: str, max_len: int = 100) -> str:
    """对命令进行 URL 编码，并严格保证编码后长度不超过 max_len（QQ 官方限制 100 字符）。"""
    cur_cmd = cmd.strip("\r\n")
    encoded = urllib.parse.quote(cur_cmd)
    if len(encoded) <= max_len:
        return encoded
    while cur_cmd and len(encoded) > max_len:
        cur_cmd = cur_cmd[:-1]
        encoded = urllib.parse.quote(cur_cmd)
    return encoded


def md_cmd_input(label: str, cmd_prefix: str, add_space: bool | None = None) -> str:
    """生成点击后填入聊天输入框的 QQ Markdown 交互标签。"""
    cleaned = cmd_prefix.strip()
    if add_space is None:
        add_space = not (cleaned.isdigit() or cleaned in ("取消", "退出", "q", "Q"))
    prefix = (cleaned + " ") if add_space else cleaned
    encoded = safe_quote_cmd(prefix)
    safe_show = html.escape(label.strip()[:50], quote=True)
    return f'<qqbot-cmd-input text="{encoded}" show="{safe_show}" reference="false" />'


def md_cmd_example(label: str, full_cmd: str) -> str:
    """生成点击后将完整示例填入输入框的 QQ Markdown 交互标签。"""
    encoded = safe_quote_cmd(full_cmd.strip())
    safe_show = html.escape(label.strip()[:50], quote=True)
    return f'<qqbot-cmd-input text="{encoded}" show="{safe_show}" reference="false" />'


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
    # 消息构建辅助方法 (Markdown Tips 风格)
    # --------------------------------------------------------------------------
    def _markdown_result(
        self, event: AstrMessageEvent | None, text: str
    ) -> MessageEventResult:
        """生成 Markdown 格式文本消息结果，并设置 use_markdown(True)。"""
        if (
            event is not None
            and type(event).__name__ != "MagicMock"
            and hasattr(event, "make_result")
        ):
            res = event.make_result().message(text)
        elif event is not None and hasattr(event, "plain_result"):
            res = event.plain_result(text)
        else:
            res = MessageEventResult().message(text)
        if hasattr(res, "use_markdown"):
            res.use_markdown(True)
        return res

    def _markdown_tip(
        self,
        event: AstrMessageEvent,
        title: str,
        details: list[str] | None = None,
    ) -> MessageEventResult:
        """生成标准 Markdown Tip 提示卡片。"""
        lines = [f"> 💡 **{title}**"]
        if details:
            for d in details:
                for line in str(d).splitlines():
                    lines.append(f"> {line}")
        return self._markdown_result(event, "\n".join(lines))

    def _markdown_success(
        self,
        event: AstrMessageEvent,
        title: str,
        details: list[str] | None = None,
    ) -> MessageEventResult:
        """生成标准 Markdown 成功卡片。"""
        lines = [f"> 🎉 **{title}**"]
        if details:
            for d in details:
                for line in str(d).splitlines():
                    lines.append(f"> {line}")
        return self._markdown_result(event, "\n".join(lines))

    def _markdown_warn(
        self,
        event: AstrMessageEvent,
        title: str,
        details: list[str] | None = None,
    ) -> MessageEventResult:
        """生成标准 Markdown 警告/错误卡片。"""
        lines = [f"> ⚠️ **{title}**"]
        if details:
            for d in details:
                for line in str(d).splitlines():
                    lines.append(f"> {line}")
        return self._markdown_result(event, "\n".join(lines))

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
            yield self._markdown_warn(event, "操作失败", ["该指令仅支持在群聊中使用。"])
            return

        if not self._can_manage(event):
            yield self._markdown_warn(
                event, "权限不足", ["仅本群群主、管理员或机器人管理员可配置本群唤醒词。"]
            )
            return

        clean_word = word.strip()
        cmd_add = md_cmd_input("设置本群唤醒词", "/设置本群唤醒词")
        cmd_clear = md_cmd_example("清空本群唤醒词", "/清空本群唤醒词")
        if not clean_word:
            yield self._markdown_tip(
                event,
                "请提供要设置的唤醒词",
                [
                    f"点击 {cmd_add} 并输入唤醒词（例如：`/设置本群唤醒词 小助手`）",
                    "",
                    f"⚠️ 唤醒词不能为空字符。若需要清空本群所有唤醒词，可点击 {cmd_clear}。",
                ],
            )
            return

        if len(clean_word) > 20:
            yield self._markdown_warn(event, "参数错误", ["唤醒词长度不能超过 20 个字符。"])
            return

        max_count = self._cfg_int("max_wake_words_per_group", 10)
        current_count = self.store.count_wake_words(group_id)
        cmd_del = md_cmd_input("删除本群唤醒词", "/删除本群唤醒词")
        if current_count >= max_count:
            yield self._markdown_warn(
                event,
                "数量超限",
                [
                    f"本群唤醒词数量已达上限（{max_count} 个）。",
                    f"请先点击 {cmd_del} 移除不需要的唤醒词。",
                ],
            )
            return

        added = await self.store.add_wake_word(group_id, clean_word)
        if added:
            yield self._markdown_success(
                event,
                "唤醒词添加成功",
                [
                    f"已成功为本群添加专属唤醒词：「**{clean_word}**」！",
                    f"现在在群内发送「`{clean_word}` + 内容」即可免 @ 触发机器人。",
                    "",
                    "⚠️ **重要权限提示**：",
                    "必须由群主在群机器人设置中开启「接收所有消息」权限，否则只能接收到 @消息（免 @ 唤醒词将无法被机器人接收）。",
                ],
            )
        else:
            yield self._markdown_tip(
                event,
                "无需重复添加",
                [f"唤醒词「**{clean_word}**」已存在于本群列表中。"],
            )

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
            yield self._markdown_warn(event, "操作失败", ["该指令仅支持在群聊中使用。"])
            return

        if not self._can_manage(event):
            yield self._markdown_warn(
                event, "权限不足", ["仅本群群主、管理员或机器人管理员可配置本群唤醒词。"]
            )
            return

        clean_word = word.strip()
        cmd_del = md_cmd_input("删除本群唤醒词", "/删除本群唤醒词")
        if not clean_word:
            yield self._markdown_tip(
                event,
                "请提供要删除的唤醒词",
                [f"点击 {cmd_del} 并输入要删除的唤醒词（例如：`/删除本群唤醒词 小助手`）"],
            )
            return

        cmd_list = md_cmd_example("查看本群唤醒词", "/查看本群唤醒词")
        removed = await self.store.remove_wake_word(group_id, clean_word)
        if removed:
            yield self._markdown_success(
                event, "唤醒词移除成功", [f"已成功移除本群唤醒词：「**{clean_word}**」。"]
            )
        else:
            yield self._markdown_tip(
                event,
                "唤醒词未找到",
                [
                    f"本群未配置唤醒词：「**{clean_word}**」。",
                    f"可点击 {cmd_list} 查看当前生效的唤醒词列表。",
                ],
            )

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
            yield self._markdown_warn(event, "操作失败", ["该指令仅支持在群聊中使用。"])
            return

        cmd_add = md_cmd_input("设置本群唤醒词", "/设置本群唤醒词")
        wake_words = self.store.list_wake_words(group_id)
        if not wake_words:
            yield self._markdown_tip(
                event,
                "本群专属唤醒词列表（暂无配置）",
                [
                    "当前本群唤醒词列表为空，机器人仅在被 @ 或使用全局前缀（如 `/`）时响应。",
                    f"群主/管理员可点击 {cmd_add} 为本群添加专属免 @ 唤醒词。",
                    "",
                    "⚠️ **权限提示**：使用免 @ 唤醒词需由群主在群机器人设置中开启「接收所有消息」权限，否则只能接收到 @消息。",
                ],
            )
            return

        details = ["当前群生效的免 @ 唤醒词："]
        for idx, w in enumerate(wake_words, 1):
            btn_del = md_cmd_example("删除", f"/删除本群唤醒词 {w}")
            details.append(f"{idx}. 「**{w}**」 （{btn_del}）")

        details.append("")
        details.append("💡 发送「唤醒词 + 内容」即可免 @ 触发机器人！")
        details.append("⚠️ **权限提示**：需群主在【群设置 ➔ 群机器人】开启「接收所有消息」，否则只能接收到 @消息。")

        yield self._markdown_tip(event, "本群专属唤醒词列表", details)

    @filter.command("清空本群唤醒词")
    async def clear_group_wake(self, event: AstrMessageEvent):
        """清空当前群的所有专属动态唤醒词（限管理员）。"""
        group_id = str(event.get_group_id() or "")
        if not group_id:
            yield self._markdown_warn(event, "操作失败", ["该指令仅支持在群聊中使用。"])
            return

        if not self._can_manage(event):
            yield self._markdown_warn(
                event, "权限不足", ["仅本群群主、管理员或机器人管理员可清空本群唤醒词。"]
            )
            return

        count = await self.store.clear_group(group_id)
        if count > 0:
            yield self._markdown_success(
                event, "清空成功", [f"已成功清空本群所有专属唤醒词（共移除 {count} 个）。"]
            )
        else:
            yield self._markdown_tip(event, "提示", ["本群当前暂无配置任何专属唤醒词。"])

    # --------------------------------------------------------------------------
    # 帮助指令（主触发词：帮助 唤醒词）
    # --------------------------------------------------------------------------
    @filter.command("帮助 唤醒词")
    async def group_wake_main_help(self, event: AstrMessageEvent):
        """查看分群动态唤醒词帮助说明。"""
        async for res in self._do_help(event):
            yield res

    @filter.command("帮助唤醒词")
    async def group_wake_join_help(self, event: AstrMessageEvent):
        """别名：查看分群动态唤醒词帮助说明。"""
        async for res in self._do_help(event):
            yield res

    @filter.command("唤醒词 帮助")
    async def group_wake_space_help(self, event: AstrMessageEvent):
        """别名：查看分群动态唤醒词帮助说明。"""
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
        cmd_add = md_cmd_input("设置本群唤醒词", "/设置本群唤醒词")
        cmd_del = md_cmd_input("删除本群唤醒词", "/删除本群唤醒词")
        cmd_list = md_cmd_example("查看本群唤醒词", "/查看本群唤醒词")
        cmd_clear = md_cmd_example("清空本群唤醒词", "/清空本群唤醒词")
        cmd_help = md_cmd_example("帮助 唤醒词", "/帮助 唤醒词")

        details = [
            "点击下方指令可直接填入输入框：",
            f"• {cmd_add} `<词>`：为本群添加专属免 @ 唤醒词",
            f"• {cmd_del} `<词>`：移除本群已配置的唤醒词",
            f"• {cmd_list}：查看本群所有生效的唤醒词",
            f"• {cmd_clear}：清空本群专属唤醒词（限管理员）",
            f"• {cmd_help}：查看本帮助指南",
            "",
            "⚙️ **功能特性**：",
            "1. 群间隔离：每个群唤醒词独立维护，互不干扰；",
            "2. 极速响应：基于内存读缓存毫秒级匹配，无磁盘 I/O 阻塞；",
            "3. 持久存储：基于 SQLite 跨重启自动恢复；",
            "4. 权限严密：支持 QQ 官方 author.member_role 管理员校验。",
            "",
            "⚠️ **【重要权限说明】**：",
            "QQ 机器人官方群聊默认仅推送 `@机器人` 的事件。",
            "若要使用「免 @ 唤醒词」，**必须由群主**在手机 QQ 中开启权限：",
            "👉 【群设置】 ➔ 【群机器人】 ➔ 点击本机器人 ➔ 开启【接收所有消息】",
            "（否则机器人只能接收到 @消息，唤醒词将无法被接收并触发）",
        ]

        yield self._markdown_tip(event, "分群动态唤醒词使用指南", details)

