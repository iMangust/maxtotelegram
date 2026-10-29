"""Фабрики событий maxapi с корректной валидацией типов.

Важно (выявлено при отладке maxapi 1.x):
 * поле ``Message.recipient`` принимает ТОЛЬКО экземпляр ``Recipient``
   (или словарь с обязательными ``chat_id`` + ``chat_type``), а не ``None``;
 * поле ``Message.timestamp`` — целое unix-время (``int``), а не словарь;
 * ``MessageBody.mid`` — строка;
 * тип вложения — значения enum в нижнем регистре (``image``, ``file``...).
"""

from __future__ import annotations

from typing import Any

from maxapi.enums.chat_type import ChatType
from maxapi.types import (
    Attachment,
    Chat,
    Message,
    MessageBody,
    MessageCreated,
    Recipient,
    User,
)


def make_user(
    user_id: int = 5,
    first_name: str = "Пётр",
    last_name: str = "Иванов",
    is_bot: bool = False,
    username: str | None = None,
) -> User:
    return User(
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        is_bot=is_bot,
        username=username,
    )


def make_recipient(chat_id: int = 999, chat_type: ChatType = ChatType.CHAT) -> Recipient:
    return Recipient(chat_id=chat_id, chat_type=chat_type)


def make_attachment(att_type: str = "image", url: str = "https://max.example/f.bin") -> Attachment:
    return Attachment.model_validate({"type": att_type, "payload": {"url": url}})


def make_message(
    *,
    mid: str = "123",
    text: str = "hello",
    chat_id: int = 999,
    sender: User | None = None,
    attachments: list[Any] | None = None,
    timestamp: int = 1_700_000_000,
) -> Message:
    body = MessageBody(mid=mid, seq=1, text=text, attachments=attachments or [])
    return Message(
        sender=sender if sender is not None else make_user(),
        recipient=make_recipient(chat_id),  # именно Recipient, не None!
        timestamp=timestamp,               # именно int, не dict!
        body=body,
    )


def make_event(
    *,
    mid: str = "123",
    text: str = "hello",
    chat_id: int = 999,
    sender: User | None = None,
    attachments: list[Any] | None = None,
    chat_title: str | None = "Рабочий чат",
    bot: Any | None = None,
) -> MessageCreated:
    message = make_message(
        mid=mid,
        text=text,
        chat_id=chat_id,
        sender=sender,
        attachments=attachments,
    )
    event = MessageCreated(timestamp=message.timestamp, message=message, bot=bot)
    # maxapi обогащает событие объектом чата отдельным API-запросом;
    # в тестах подставляем его напрямую.
    # Важно: у Chat обязательные поля status / last_event_time /
    # participants_count / is_public (проверено на maxapi 1.x).
    event.chat = Chat(
        chat_id=chat_id,
        type="chat",
        status="active",
        last_event_time=message.timestamp,
        participants_count=2,
        is_public=False,
        title=chat_title,
    )
    event.from_user = message.sender
    return event
