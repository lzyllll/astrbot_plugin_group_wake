from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SqliteGroupWakeStore:
    """分群唤醒词 SQLite 持久化与内存读缓存管理器。

    设计特性：
    1. 读写分离：内存字典作为读缓存，每条消息唤醒匹配零 I/O 开销，微秒级延迟；
    2. 异步写盘：增删操作通过 asyncio.to_thread 在后台线程池执行，不阻塞事件循环；
    3. 并发安全：写操作受 asyncio.Lock 互斥保护，避免并发写入冲突。
    """

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._cache: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    async def init(self) -> None:
        """初始化数据库结构并全量预热内存缓存。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._sync_init_and_load)

    def _sync_init_and_load(self) -> None:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS group_wake_words (
                        group_id TEXT NOT NULL,
                        wake_word TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY (group_id, wake_word)
                    );
                    """
                )
                cursor.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_group_wake_words_gid
                    ON group_wake_words (group_id);
                    """
                )
                conn.commit()

                cursor.execute("SELECT group_id, wake_word FROM group_wake_words")
                rows = cursor.fetchall()
                new_cache: dict[str, set[str]] = {}
                for gid, word in rows:
                    new_cache.setdefault(str(gid), set()).add(str(word))
                self._cache = new_cache
        finally:
            conn.close()

    def get_wake_words(self, group_id: str) -> set[str]:
        """获取指定群的全部唤醒词集合（零 I/O 内存操作）。"""
        words = self._cache.get(str(group_id))
        return set(words) if words else set()

    def list_wake_words(self, group_id: str) -> list[str]:
        """按长度降序和字母顺序获取指定群的唤醒词列表。"""
        words = self.get_wake_words(group_id)
        return sorted(words, key=lambda w: (-len(w), w))

    def count_wake_words(self, group_id: str) -> int:
        """获取指定群当前的唤醒词数量。"""
        words = self._cache.get(str(group_id))
        return len(words) if words else 0

    async def add_wake_word(self, group_id: str, word: str) -> bool:
        """为指定群添加专属唤醒词。若已存在则返回 False。"""
        gid = str(group_id).strip()
        w = word.strip()
        if not gid or not w:
            return False

        if w in self._cache.get(gid, set()):
            return False

        async with self._lock:
            now_iso = _utc_now()
            inserted = await asyncio.to_thread(self._sync_insert, gid, w, now_iso)
            if inserted:
                self._cache.setdefault(gid, set()).add(w)
            return inserted

    def _sync_insert(self, group_id: str, word: str, created_at: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO group_wake_words (group_id, wake_word, created_at)
                    VALUES (?, ?, ?)
                    """,
                    (group_id, word, created_at),
                )
                conn.commit()
                return cursor.rowcount > 0
        finally:
            conn.close()

    async def remove_wake_word(self, group_id: str, word: str) -> bool:
        """从指定群移除专属唤醒词。若不存在则返回 False。"""
        gid = str(group_id).strip()
        w = word.strip()
        if not gid or not w:
            return False

        if gid not in self._cache or w not in self._cache[gid]:
            return False

        async with self._lock:
            deleted = await asyncio.to_thread(self._sync_delete, gid, w)
            if gid in self._cache:
                self._cache[gid].discard(w)
                if not self._cache[gid]:
                    self._cache.pop(gid, None)
            return deleted

    def _sync_delete(self, group_id: str, word: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM group_wake_words WHERE group_id = ? AND wake_word = ?",
                    (group_id, word),
                )
                conn.commit()
                return cursor.rowcount > 0
        finally:
            conn.close()

    async def clear_group(self, group_id: str) -> int:
        """清空指定群的所有专属唤醒词，返回删除的数量。"""
        gid = str(group_id).strip()
        if not gid:
            return 0

        async with self._lock:
            count = await asyncio.to_thread(self._sync_clear, gid)
            self._cache.pop(gid, None)
            return count

    def _sync_clear(self, group_id: str) -> int:
        conn = self._connect()
        try:
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM group_wake_words WHERE group_id = ?",
                    (group_id,),
                )
                conn.commit()
                return cursor.rowcount
        finally:
            conn.close()
