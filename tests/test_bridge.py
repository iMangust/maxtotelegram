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


class FakeStore:
    def __init__(self) -> None:
        self.map: dict[str, tuple[int, int]] = {}

    async def get_tg_by_max(self, mid):
        return self.map.get(str(mid))

    async def remember(self, mid, tg_id, chat_id):
        self.map[str(mid)] = (tg_id, chat_id)


def _settings(**kw) -> Settings:
    base = dict(max_token="m", tg_token="t", default_tg_chat=-100)
    base.update(kw)
    return Settings(**base)


async def _dispatch(router, event) -> None:
    """Вызывает обработчик message_created напрямую.

    В maxapi 1.x хендлеры хранятся в ``router.event_handlers``
    (объект ``Handler`` с полем ``func_event``); ``observers`` у Router нет.
    """
    handler_obj = router.event_handlers[-1]
    func = getattr(handler_obj, "func_event", handler_obj)
    await func(event)


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
