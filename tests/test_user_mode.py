"""Тесты user-режима (личный аккаунт MAX через WebSocket)."""

from __future__ import annotations

import pytest

from bridge.config import ConfigError, load_settings
from bridge.user_client import MaxUserClient, UserMessage
from bridge.user_listener import build_user_listener, should_forward_user


def _settings(**env):
    import os
    old = dict(os.environ)
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return load_settings(env_file="/nonexistent.env")
    finally:
        os.environ.clear()
        os.environ.update(old)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("MAX_MODE", "MAX_BOT_TOKEN", "MAX_USER_TOKEN", "MAX_DEVICE_ID",
                "TG_BOT_TOKEN", "TG_CHAT_ID", "CHAT_MAP", "MAX_CHAT_IDS",
                "MAX_EXCLUDE_CHAT_IDS", "BRIDGE_DOTENV"):
        monkeypatch.delenv(var, raising=False)


def test_auto_mode_selects_user():
    s = _settings(MAX_USER_TOKEN="t", MAX_DEVICE_ID="d",
                  TG_BOT_TOKEN="x", TG_CHAT_ID="123")
    assert s.mode == "user"
    assert s.max_user_token == "t" and s.max_device_id == "d"


def test_auto_mode_falls_back_to_bot():
    s = _settings(MAX_BOT_TOKEN="b", TG_BOT_TOKEN="x", TG_CHAT_ID="123")
    assert s.mode == "bot"


def test_explicit_user_mode_requires_creds():
    with pytest.raises(ConfigError):
        _settings(MAX_MODE="user", TG_BOT_TOKEN="x", TG_CHAT_ID="123")


def test_parse_message_and_is_self():
    client = MaxUserClient(token="t", device_id="d")
    client.my_id = 42
    msg = client._parse_message({
        "chatId": 100,
        "message": {"id": 555, "sender": 7, "text": "Привет", "time": 1},
    })
    assert msg is not None
    assert msg.chat_id == 100 and msg.text == "Привет"
    assert msg.message_id == "555" and msg.is_self is False
    own = client._parse_message({
        "chatId": 100,
        "message": {"id": 556, "sender": 42, "text": "сам"},
    })
    assert own.is_self is True
    # редактирование (updateTime) не порождает нового сообщения
    assert client._parse_message({"message": {"id": 1, "updateTime": 9}}).update_time == 9


def test_load_snapshot_chats_and_types():
    client = MaxUserClient(token="t", device_id="d")
    client.load_snapshot({
        "profile": {"contact": {"id": 42, "names": [{"firstName": "Я", "lastName": ""}]}},
        "chats": [
            {"id": 100, "type": "DIALOG", "title": None, "participants": {"7": {}, "42": {}}},
            {"id": -5, "type": "CHAT", "title": "Рабочий чат", "participants": {}},
        ],
    })
    assert client.my_id == 42
    assert client.chat_types[100] == "DIALOG" and client.chat_types[-5] == "CHAT"
    assert client.chats[-5] == "Рабочий чат"
    assert client.users[42] == "Я"


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_plain(self, chat_id, text):
        self.sent.append(("plain", chat_id, text)); return 1

    async def send_forwarded(self, chat_id, header, text, attachments=None):
        self.sent.append(("fwd", chat_id, header, text, len(attachments or [])))
        return 900 + len(self.sent)


class FakeStore:
    def __init__(self):
        self.seen = set()
        self.chats = []

    async def get_tg_by_max(self, mid):
        return 5 if mid in self.seen else None

    async def remember(self, mid, tg_id, target):
        self.seen.add(mid)

    async def remember_chat(self, cid, title=None, kind="chat"):
        self.chats.append((cid, title, kind))


class _S:
    default_tg_chat = 777
    owner_chat_id = None
    exclude_chats = frozenset()
    user_chat_ids = frozenset()

    def telegram_target(self, cid):
        return 777


@pytest.mark.asyncio
async def test_user_message_forwarded_once_with_header():
    client = MaxUserClient(token="t", device_id="d")
    client.my_id = 42
    client.chat_types[100] = "DIALOG"
    client.users[7] = "Иван Петров"
    store = FakeStore()
    sender = FakeSender()
    build_user_listener(_S(), sender, store, client)  # type: ignore[arg-type]

    msg = UserMessage(chat_id=100, sender_id=7, text="Здравствуйте!", message_id="1")
    await client._on_message_cb(msg)
    assert sender.sent[0][0] == "fwd" and sender.sent[0][1] == 777
    assert "Иван Петров" in sender.sent[0][2]
    assert "Здравствуйте!" in sender.sent[0][3]
    # дубль того же id не пересылается
    await client._on_message_cb(msg)
    assert len(sender.sent) == 1
    # свои сообщения игнорируются
    await client._on_message_cb(UserMessage(chat_id=100, sender_id=42, text="сам", message_id="2", is_self=True))
    assert len(sender.sent) == 1


def test_should_forward_user_filters():
    client = MaxUserClient(token="t", device_id="d")
    m = lambda cid, self=False: UserMessage(chat_id=cid, message_id="x", is_self=self)
    assert should_forward_user(client, m(1), set(), set()) is True
    assert should_forward_user(client, m(1, self=True), set(), set()) is False
    assert should_forward_user(client, m(1), {"1"}, set()) is False
    assert should_forward_user(client, m(1), set(), {"2"}) is False
    assert should_forward_user(client, m(2), set(), {"2"}) is True
