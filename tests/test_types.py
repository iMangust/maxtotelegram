"""Тесты конвертации типов maxapi (recipient/timestamp/attachments)."""

from __future__ import annotations

import pytest
from maxapi.enums.chat_type import ChatType
from maxapi.types import Message, MessageBody, Recipient
from pydantic import ValidationError

from bridge.convert import attachment_kind, payload_download_info
from tests.helpers import make_attachment, make_event, make_message, make_recipient


class TestMessageValidation:
    def test_recipient_must_be_recipient_instance(self) -> None:
        """recipient=None недопустим — поле обязательное и типизированное."""
        with pytest.raises(Exception):
            Message(
                sender=None,
                recipient=None,
                timestamp=1_700_000_000,
                body=MessageBody(mid="1", seq=1),
            )

    def test_recipient_from_dict_requires_chat_type(self) -> None:
        with pytest.raises(Exception):
            Recipient.model_validate({"chat_id": 1})  # без chat_type
        r = Recipient.model_validate({"chat_id": 1, "chat_type": "chat"})
        assert r.chat_id == 1
        assert r.chat_type == ChatType.CHAT

    def test_timestamp_must_be_int_not_dict(self) -> None:
        body = MessageBody(mid="1", seq=1, text="hi")
        with pytest.raises(Exception):
            Message.model_validate(
                {
                    "recipient": {"chat_id": 1, "chat_type": "chat"},
                    "timestamp": {"time": 1_700_000_000},  # словарь — ошибка
                    "body": body.model_dump(),
                }
            )
        msg = Message.model_validate(
            {
                "recipient": {"chat_id": 1, "chat_type": "chat"},
                "timestamp": 1_700_000_000,
                "body": body.model_dump(),
            }
        )
        assert isinstance(msg.timestamp, int)

    def test_mid_is_string(self) -> None:
        # maxapi использует строгую типизацию pydantic (без coercion):
        # int в mid приводит к ValidationError. Фиксируем это поведение.
        with pytest.raises(ValidationError):
            MessageBody.model_validate({"mid": 123, "seq": 1})
        body = MessageBody.model_validate({"mid": "123", "seq": 1})
        assert body.mid == "123"

    def test_make_helpers_validate(self) -> None:
        msg = make_message(text="{{Ivanov}} привет")
        assert isinstance(msg.recipient, Recipient)
        assert isinstance(msg.timestamp, int)
        event = make_event()
        assert event.message.recipient.chat_id == 999


class TestAttachments:
    def test_attachment_type_lowercase(self) -> None:
        att = make_attachment("image")
        assert str(att.type) == "image" or getattr(att.type, "value", "") == "image"
        url, _ = payload_download_info(att.payload)
        assert url == "https://max.example/f.bin"

    @pytest.mark.parametrize(
        ("raw", "kind"),
        [
            ("image", "photo"),
            ("video", "video"),
            ("file", "doc"),
            ("sticker", "photo"),
            ("audio", "doc"),
            ("unknown", "other"),
            ("", "other"),
        ],
    )
    def test_attachment_kind(self, raw: str, kind: str) -> None:
        assert attachment_kind(raw) == kind

    def test_recipient_defaults(self) -> None:
        r = make_recipient(chat_id=42)
        assert r.user_id is None
        assert r.chat_id == 42
