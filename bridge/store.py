"""Хранилище соответствий сообщений MAX <-> Telegram.

Нужно для:
 * защиты от эха (пересланное сообщение не должно пересылаться обратно);
 * возможности редактирования/удаления уже пересланных сообщений;
 * команды /status.

По умолчанию используется SQLite (подходит и для одного процесса, и для
нескольких воркеров). Для тестов можно передать in-memory базу (":memory:").
"""

from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message_map (
    max_msg_id   TEXT PRIMARY KEY,
    tg_msg_id    INTEGER NOT NULL,
    tg_chat_id   INTEGER NOT NULL,
    created_at   TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tg_msg ON message_map (tg_msg_id);
"""


class MessageStore:
    """Двустороннее отображение идентификаторов сообщений."""

    def __init__(self, db_path: str = "bridge.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def _conn(self) -> aiosqlite.Connection:
        assert self._db is not None, "MessageStore.connect() не вызван"
        return self._db

    async def remember(
        self, max_msg_id: str, tg_msg_id: int, tg_chat_id: int
    ) -> None:
        """Сохраняет пару: сообщение в MAX -> сообщение в Telegram."""
        await self._conn.execute(
            "INSERT OR REPLACE INTO message_map (max_msg_id, tg_msg_id, tg_chat_id)"
            " VALUES (?, ?, ?)",
            (str(max_msg_id), int(tg_msg_id), int(tg_chat_id)),
        )
        await self._conn.commit()

    async def get_tg_by_max(self, max_msg_id: str) -> tuple[int, int] | None:
        """Возвращает (tg_message_id, tg_chat_id) по id сообщения MAX."""
        async with self._conn.execute(
            "SELECT tg_msg_id, tg_chat_id FROM message_map WHERE max_msg_id = ?",
            (str(max_msg_id),),
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row[0]), int(row[1])) if row else None

    async def get_max_by_tg(self, tg_msg_id: int) -> str | None:
        """Возвращает id сообщения MAX по id сообщения Telegram."""
        async with self._conn.execute(
            "SELECT max_msg_id FROM message_map WHERE tg_msg_id = ?",
            (int(tg_msg_id),),
        ) as cursor:
            row = await cursor.fetchone()
        return str(row[0]) if row else None

    async def forget_by_max(self, max_msg_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM message_map WHERE max_msg_id = ?", (str(max_msg_id),)
        )
        await self._conn.commit()

    async def count(self) -> int:
        async with self._conn.execute("SELECT COUNT(*) FROM message_map") as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else 0
