"""Интеграционные тесты обработчика MAX -> Telegram (без сети).

Проверяют сквозной путь: событие maxapi -> конвертация -> "отправка" в TG
через мок TelegramSender -> запись маппинга в мок store.
"""

from __future__ import annotations

import pytest
from maxapi.types import MessageCreated

from bridge.config import Settings
from bridge.max_listener import build_max_router
from tests.helpers import make_attachment, make_event, make_user


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, str, list]] = []

    async def send_forwarded(self, chat_id, header, text_html, attachments):
        self.sent.append((chat_id, header, text_html, attachments))
        return 5000 + len(self.sent)

    async def send_plain(self, chat_id, text_html):
        return None


class FakeStore:
    def __init__(self) -> None:
        self.map: dict[str, tuple[int, int]] = {}
        self.chats: dict[str, tuple[str | None, str]] = {}

    async def get_tg_by_max(self, mid):
        return self.map.get(str(mid))

    async def remember(self, mid, tg_id, chat_id):
        self.map[str(mid)] = (tg_id, chat_id)

    async def remember_chat(self, chat_id, title=None, kind="chat"):
        self.chats[str(chat_id)] = (title, kind)

    async def forget_chat(self, chat_id):
        self.chats.pop(str(chat_id), None)

    async def known_chat_ids(self):
        return set(self.chats)

    async def list_chats(self):
        return [(cid, t, k, None) for cid, (t, k) in self.chats.items()]


def _settings(**kw) -> Settings:
    base = dict(max_token="m", tg_token="t", default_tg_chat=-100)
    base.update(kw)
    return Settings(**base)


async def _dispatch(router, event) -> None:
    """Вызывает обработчик message_created напрямую.

    В maxapi 1.x все хендлеры лежат в ``router.event_handlers`` (объект
    ``Handler`` с полем ``func_event``); ``observers`` у Router нет. Ищем
    именно message_created — остальные хендлеры (bot_added и т.п.) здесь
    неуместны.
    """
    for handler_obj in router.event_handlers:
        inner = getattr(handler_obj, "func_event", handler_obj)
        if getattr(inner, "__name__", "") == "on_message_created":
            await inner(event)
            return
    raise AssertionError("handler on_message_created not found")


@pytest.mark.asyncio
async def test_text_message_is_forwarded() -> None:
    sender, store = FakeSender(), FakeStore()
    router = build_max_router(_settings(), sender, store)  # type: ignore[arg-type]
    event = make_event(mid="aaa", text="{{Ivanov}} привет", chat_id=999)

    await _dispatch(router, event)

    assert len(sender.sent) == 1
    chat_id, header, text_html, attachments = sender.sent[0]
    assert chat_id == -100
    assert "Пётр Иванов" in header
    assert "Рабочий чат" in header
    assert "<b>привет</b>" not in text_html  # текст экранирован, не тегирован наугад
    assert "привет" in text_html
    assert attachments == []
    assert store.map == {"aaa": (5001, -100)}


@pytest.mark.asyncio
async def test_duplicate_is_skipped() -> None:
    sender, store = FakeSender(), FakeStore()
    router = build_max_router(_settings(), sender, store)  # type: ignore[arg-type]
    event = make_event(mid="dup", text="раз")

    await _dispatch(router, event)
    await _dispatch(router, event)

    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_bot_message_filtered_out() -> None:
    sender, store = FakeSender(), FakeStore()
    router = build_max_router(_settings(), sender, store)  # type: ignore[arg-type]
    bot_user = make_user(is_bot=True)
    event = make_event(mid="bot1", text="автоответ", sender=bot_user)

    # Фильтр NotBotMessage стоит в декораторе; проверяем его логику напрямую.
    from bridge.max_listener import NotBotMessage

    assert await NotBotMessage()(event) is False


@pytest.mark.asyncio
async def test_unmapped_chat_without_default_is_skipped() -> None:
    sender, store = FakeSender(), FakeStore()
    settings = Settings(
        max_token="m", tg_token="t", chat_map={"111": -100}, default_tg_chat=None
    )
    router = build_max_router(settings, sender, store)  # type: ignore[arg-type]
    event = make_event(mid="x", text="привет", chat_id=999)

    await _dispatch(router, event)

    assert sender.sent == []
    assert store.map == {}


@pytest.mark.asyncio
async def test_empty_message_is_skipped() -> None:
    sender, store = FakeSender(), FakeStore()
    router = build_max_router(_settings(), sender, store)  # type: ignore[arg-type]
    event = make_event(mid="empty", text="   ")

    await _dispatch(router, event)

    assert sender.sent == []


@pytest.mark.asyncio
async def test_chat_registry_updated_on_message() -> None:
    """Каждое сообщение пополняет реестр чатов (замена удалённому GET /chats)."""
    sender, store = FakeSender(), FakeStore()
    router = build_max_router(_settings(), sender, store)  # type: ignore[arg-type]
    event = make_event(mid="reg1", text="привет", chat_id=777)

    await _dispatch(router, event)

    assert "777" in store.chats
    assert store.chats["777"][0] == "Рабочий чат"


def test_should_forward_rules() -> None:
    from bridge.max_listener import should_forward

    personal = _settings()  # только default_tg_chat — личный режим
    assert should_forward(personal, "1", "chat") is True
    assert should_forward(personal, "1", "dialog") is True

    selective = Settings(
        max_token="m", tg_token="t", chat_map={"111": -100}, default_tg_chat=None
    )
    assert should_forward(selective, "111", "chat") is True
    assert should_forward(selective, "222", "chat") is False
    assert should_forward(selective, "222", "dialog") is True  # личное — всегда

    manual = _settings(forward_chats=frozenset({"555"}))
    assert should_forward(manual, "555", "chat") is True

    nomap = Settings(max_token="m", tg_token="t", chat_map={"111": -1})
    # chat_map без default и FORWARD_ALL_CHATS: чат вне списка не шлём
    assert should_forward(nomap, "999", "chat") is False


@pytest.mark.asyncio
async def test_store_chat_registry_sqlite(tmp_path) -> None:
    from bridge.store import MessageStore

    store = MessageStore(str(tmp_path / "t.db"))
    await store.connect()
    await store.remember_chat(123, title="Чатик", kind="chat")
    await store.remember_chat(123, title=None, kind="chat")  # не затирает title
    assert await store.known_chat_ids() == {"123"}
    chats = await store.list_chats()
    assert chats[0][0] == "123" and chats[0][1] == "Чатик"
    await store.forget_chat(123)
    assert await store.known_chat_ids() == set()
    await store.close()
