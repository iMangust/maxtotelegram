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
CREATE TABLE IF NOT EXISTS known_chats (
    chat_id      TEXT PRIMARY KEY,
    title        TEXT,
    kind         TEXT DEFAULT 'chat',
    added_at     TEXT DEFAULT (datetime('now')),
    last_seen_at TEXT
);
"""


class MessageStore:
    """Двустороннее отображение идентификаторов сообщений + реестр чатов MAX.

    Таблица ``known_chats`` решает ограничение Bot API MAX: с июня 2026 года
    ``GET /chats`` не поддерживается и готового списка чатов нет — бот должен
    накапливать ``chat_id`` сам из событий ``bot_added`` / ``bot_started`` /
    ``message_created`` (см. докстринг ``maxapi.methods.get_chats``).
    """

    def __init__(self, db_path: str = "bridge.db") -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._db_path)
        # Совместимость со старыми базами: добавляем колонки, если их нет.
        await self._db.executescript(_SCHEMA)
        for col, decl in (("title", "TEXT"), ("kind", "TEXT DEFAULT 'chat'")):
            try:
                await self._db.execute(
                    f"ALTER TABLE known_chats ADD COLUMN {col} {decl}"
                )
            except aiosqlite.OperationalError:
                pass  # колонка уже есть
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

    # --- Реестр чатов MAX ----------------------------------------------------

    async def remember_chat(
        self, chat_id: str | int, title: str | None = None, kind: str = "chat"
    ) -> None:
        """Заносит (или обновляет) чат MAX в реестр известных чатов."""
        await self._conn.execute(
            """
            INSERT INTO known_chats (chat_id, title, kind, last_seen_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(chat_id) DO UPDATE SET
                last_seen_at = datetime('now'),
                title        = COALESCE(excluded.title, known_chats.title),
                kind         = excluded.kind
            """,
            (str(chat_id), title, kind),
        )
        await self._conn.commit()

    async def forget_chat(self, chat_id: str | int) -> None:
        """Убирает чат из реестра (события bot_removed / bot_stopped)."""
        await self._conn.execute(
            "DELETE FROM known_chats WHERE chat_id = ?", (str(chat_id),)
        )
        await self._conn.commit()

    async def known_chat_ids(self) -> set[str]:
        async with self._conn.execute("SELECT chat_id FROM known_chats") as cursor:
            rows = await cursor.fetchall()
        return {str(row[0]) for row in rows}

    async def list_chats(self) -> list[tuple[str, str | None, str, str | None]]:
        """Возвращает [(chat_id, title, kind, last_seen_at), ...]."""
        async with self._conn.execute(
            "SELECT chat_id, title, kind, last_seen_at FROM known_chats"
            " ORDER BY added_at"
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            (str(r[0]), r[1], r[2] or "chat", r[3]) for r in rows
        ]
