"""Неофициальный клиент MAX от имени пользователя (userbot-режим).

В отличие от Bot API (``https://dev.max.ru``, только события для бота),
это тот же протокол, что использует веб-клиент ``web.max.ru``: WebSocket
``wss://ws-api.oneme.ru/websocket`` с авторизацией по токену сессии вашего
личного аккаунта. Поэтому приложение видит ВСЕ ваши диалоги — включая
личные сообщения от незнакомых людей — без добавления какого-либо бота.

Токен и device ID берутся из Local Storage браузера на web.max.ru:
  * ``__oneme_auth`` -> поле ``token``  -> MAX_USER_TOKEN
  * ``__oneme_device_id``              -> MAX_DEVICE_ID

ПРЕДУПРЕЖДЕНИЕ: это реверс-инжиниринг неофициального клиента. Он нарушает
пользовательское соглашение MAX и может привести к блокировке аккаунта;
используйте на свой страх и риск (приоритетный режим — bot, см. config.py).

Протокол и парсинг сообщений основаны на проекте Aist/max2tg (MIT-подобная
лицензия, см. NOTICE).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import aiohttp

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

_WS_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Cache-Control": "no-cache",
}

_HTTP_HEADERS = {
    **_BROWSER_HEADERS,
    "Origin": "https://web.max.ru",
    "Referer": "https://web.max.ru/",
    "Accept": "*/*",
}


def safe_url(url: Any) -> str:
    """Убирает query-строку (подписанные токены доступа) из URL для логов."""
    if not url:
        return str(url)
    try:
        parts = urlsplit(str(url))
    except ValueError:
        return "<unparsable url>"
    if not parts.scheme and not parts.netloc:
        return "<non-url>"
    shown = f"{parts.scheme}://{parts.netloc}{parts.path}"
    return f"{shown}?…" if parts.query else shown


class OpCode(IntEnum):
    HEARTBEAT_PING = 1
    HANDSHAKE = 6
    AUTH_SNAPSHOT = 19
    CONTACT_GET = 32
    GET_MESSAGES = 49
    SEND_MESSAGE = 64
    GET_VIDEO_URL = 83
    FILE_UPLOAD = 87
    GET_FILE_URL = 88
    DISPATCH = 128
    NOTIF_ATTACH = 136


@dataclass(slots=True)
class UserMessage:
    """Сообщение, полученное от имени пользователя."""

    chat_id: Any = None
    sender_id: Any = None
    text: str = ""
    timestamp: Any = None
    message_id: str = ""
    is_self: bool = False
    attaches: list = field(default_factory=list)
    link: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    update_time: Any = None


class MaxUserClient:
    """WebSocket-клиент личного аккаунта MAX (userbot)."""

    WS_URL = "wss://ws-api.oneme.ru/websocket"
    HEARTBEAT_SEC = 30
    RECONNECT_SEC = 5

    def __init__(self, token: str, device_id: str) -> None:
        self.token = token
        self.device_id = device_id
        self.my_id: Any = None
        self.chat_types: dict[Any, str] = {}
        self.chats: dict[Any, str] = {}
        self.users: dict[Any, str] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._heartbeat_task: asyncio.Task | None = None
        self._on_ready_cb: Callable[[dict], Awaitable[None]] | None = None
        self._on_message_cb: Callable[[UserMessage], Awaitable[None]] | None = None
        self._on_disconnect_cb: Callable[[], Awaitable[None]] | None = None
        self._first_connect = True

    # ── callback registration ──────────────────────────────────────

    def on_ready(self, func):
        self._on_ready_cb = func
        return func

    def on_message(self, func):
        self._on_message_cb = func
        return func

    def on_disconnect(self, func):
        self._on_disconnect_cb = func
        return func

    # ── transport ──────────────────────────────────────────────────

    async def _send(self, opcode: int, payload: dict) -> int:
        assert self._ws is not None, "Нет соединения"
        self._seq += 1
        body = {"opcode": opcode, "seq": self._seq, "payload": payload}
        await self._ws.send_str(json.dumps(body))
        return self._seq

    async def cmd(self, opcode: int, payload: dict, timeout: float = 10) -> dict:
        """Запрос-ответ через WebSocket (RPC)."""
        if self._ws is None:
            return {}
        seq = await self._send(opcode, payload)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(seq, None)
            return {}

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.HEARTBEAT_SEC)
            try:
                await self._send(OpCode.HEARTBEAT_PING, {})
            except Exception:
                return

    async def run(self) -> None:
        """Бесконечный цикл подключения с автопереподключением."""
        async with aiohttp.ClientSession(headers=_BROWSER_HEADERS) as session:
            self._session = session
            while True:
                try:
                    logger.info("MAX user: подключение к %s ...", self.WS_URL)
                    async with session.ws_connect(
                        self.WS_URL, headers=_WS_HEADERS
                    ) as ws:
                        self._ws = ws
                        self._seq = 0
                        self._pending.clear()
                        await self._send(
                            OpCode.HANDSHAKE,
                            {
                                "deviceId": self.device_id,
                                "userAgent": {
                                    "deviceType": "WEB",
                                    "deviceName": "Chrome 131.0.0.0",
                                    "headerUserAgent": _USER_AGENT,
                                    "appVersion": "26.4.7",
                                },
                            },
                        )
                        self._heartbeat_task = asyncio.create_task(
                            self._heartbeat_loop()
                        )
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                await self._handle(json.loads(msg.data))
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                break
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("MAX user: ошибка соединения")
                finally:
                    if self._heartbeat_task:
                        self._heartbeat_task.cancel()
                    for fut in self._pending.values():
                        if not fut.done():
                            fut.cancel()
                    self._pending.clear()

                if self._on_disconnect_cb:
                    try:
                        await self._on_disconnect_cb()
                    except Exception:
                        logger.exception("MAX user: ошибка on_disconnect")
                logger.info(
                    "MAX user: переподключение через %ds...", self.RECONNECT_SEC
                )
                await asyncio.sleep(self.RECONNECT_SEC)

    # ── event dispatcher ───────────────────────────────────────────

    async def _handle(self, data: dict) -> None:
        op = data.get("opcode")
        cmd = data.get("cmd")
        seq = data.get("seq")
        payload = data.get("payload", {})

        if cmd == 1 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result(payload)
        elif cmd == 3 and seq in self._pending:
            fut = self._pending.pop(seq)
            if not fut.done():
                fut.set_result({})
            logger.warning(
                "MAX user: ERROR op=%s seq=%s | %s",
                op,
                seq,
                self._mask_sensitive(str(payload))[:300],
            )
        elif op == OpCode.HANDSHAKE and cmd == 1:
            logger.info("MAX user: handshake OK → авторизация...")
            await self._send(
                OpCode.AUTH_SNAPSHOT,
                {"chatsCount": 10, "interactive": True, "token": self.token},
            )
        elif op == OpCode.AUTH_SNAPSHOT and cmd == 1:
            self.load_snapshot(payload)
            reconnecting = not self._first_connect
            self._first_connect = False
            if self._on_ready_cb:
                task = asyncio.create_task(self._on_ready_cb(payload, reconnecting))
                task.add_done_callback(self._log_task_exception)
            if reconnecting:
                # Добираем сообщения, присланные во время разрыва.
                task = asyncio.create_task(self.backfill_recent())
                task.add_done_callback(self._log_task_exception)
        elif op == OpCode.DISPATCH:
            self.process_message(payload)

    async def backfill_recent(self) -> None:
        """После переподключения добирает сообщения из известных чатов за
        время разрыва (иначе они теряются — DISPATCH приходит только живые)."""
        since = (int(time.time()) - 300) * 1000
        for cid in list(self.chats.keys()):
            resp = await self.cmd(
                OpCode.GET_MESSAGES,
                {
                    "chatId": cid,
                    "from": since,
                    "forward": 20,
                    "backward": 0,
                    "getMessages": True,
                },
            )
            for message in resp.get("messages", []) or []:
                self.process_message({"message": message, "chatId": cid})

    @staticmethod
    def _log_task_exception(task: "asyncio.Task") -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.exception(
                "MAX user: необработанное исключение в задаче",
                exc_info=task.exception(),
            )

    # ── snapshot / contacts ────────────────────────────────────────

    def load_snapshot(self, snapshot: dict) -> None:
        """Разбирает AUTH_SNAPSHOT: свой id, чаты, типы чатов, имена."""
        profile = snapshot.get("profile", {}).get("contact", {})
        self.my_id = profile.get("id")
        names = profile.get("names", [])
        if names and self.my_id:
            n = names[0]
            full = f"{n.get('firstName', '')} {n.get('lastName', '')}".strip()
            self.users[self.my_id] = full or n.get("name", "")

        peers: list[Any] = []
        for chat in snapshot.get("chats", []):
            cid = chat.get("id")
            ctype = chat.get("type")
            title = chat.get("title")
            if cid is None:
                continue
            if ctype:
                self.chat_types[cid] = ctype
            if title:
                self.chats[cid] = title
            participants = chat.get("participants", {})
            if not title and ctype == "DIALOG" and self.my_id:
                for uid in participants:
                    try:
                        uid_int = int(uid)
                    except (ValueError, TypeError):
                        continue
                    if uid_int != self.my_id:
                        self.chats[cid] = f"DM:{uid_int}"
                        peers.append(uid_int)
                        break
        # Имена собеседников резолвим фоном (RPC CONTACT_GET), чтобы не
        # блокировать загрузку снимка.
        if peers:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None  # вне event loop (например, в тестах) — пропускаем
            if loop is not None:
                task = loop.create_task(self.resolve_users_batch(peers))
                task.add_done_callback(self._log_task_exception)
        logger.info(
            "MAX user: снимок загружен: my_id=%s, чатов=%d",
            self.my_id,
            len(self.chats),
        )

    async def resolve_users_batch(self, user_ids: list) -> None:
        unknown = [u for u in user_ids if u not in self.users]
        if unknown:
            resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": unknown})
            self._extract_contacts(resp)

    async def resolve_user(self, user_id: Any) -> str:
        """Возвращает имя пользователя по id (RPC CONTACT_GET, opcode 32)."""
        if user_id in self.users:
            return self.users[user_id]
        resp = await self.cmd(OpCode.CONTACT_GET, {"contactIds": [user_id]})
        self._extract_contacts(resp)
        return self.users.get(user_id, str(user_id))

    def _extract_contacts(self, resp: dict) -> None:
        contacts = resp.get("contacts") or resp.get("users") or []
        if isinstance(contacts, dict):
            contacts = list(contacts.values())
        for c in contacts:
            if not isinstance(c, dict):
                continue
            uid = c.get("id") or c.get("userId")
            name = self._extract_name(c)
            if uid is not None and name:
                self.users[uid] = name

    @staticmethod
    def _extract_name(c: dict) -> str:
        names_list = c.get("names")
        if isinstance(names_list, list) and names_list:
            n = names_list[0]
            full = f"{n.get('firstName', '')} {n.get('lastName', '')}".strip()
            if full:
                return full
            if n.get("name"):
                return str(n["name"])
        first = c.get("firstName") or c.get("first_name") or ""
        last = c.get("lastName") or c.get("last_name") or ""
        if first or last:
            return f"{first} {last}".strip()
        return str(c.get("friendly") or c.get("displayName") or c.get("name") or "")

    # ── messages ───────────────────────────────────────────────────

    def process_message(self, payload: dict) -> None:
        if self._on_message_cb is None:
            return
        msg = self._parse_message(payload)
        if msg is None or msg.update_time is not None:
            return  # редактирование, а не новое сообщение
        task = asyncio.create_task(self._on_message_cb(msg))
        task.add_done_callback(self._log_task_exception)

    def _parse_message(self, payload: dict) -> UserMessage | None:
        msg_body = payload.get("message")
        if not msg_body or not isinstance(msg_body, dict):
            return None
        msg = UserMessage(
            chat_id=payload.get("chatId"),
            sender_id=msg_body.get("sender"),
            text=msg_body.get("text", ""),
            timestamp=msg_body.get("time"),
            message_id=str(msg_body.get("id", "")),
            attaches=msg_body.get("attaches") or [],
            link=msg_body.get("link") or {},
            raw=payload,
            update_time=msg_body.get("updateTime"),
        )
        if self.my_id and msg.sender_id == self.my_id:
            msg.is_self = True
        return msg

    # ── files ──────────────────────────────────────────────────────

    async def download_file(self, url: str, omit_origin: bool = False) -> bytes | None:
        session = self._session
        close_after = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(headers=_BROWSER_HEADERS)
            close_after = True
        headers = dict(_HTTP_HEADERS)
        if omit_origin:
            headers.pop("Origin", None)
        try:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=120)
            ) as resp:
                if resp.status == 200:
                    return await resp.read()
                logger.warning(
                    "MAX user: скачивание %s — HTTP %d", safe_url(url), resp.status
                )
        except Exception:
            logger.exception("MAX user: ошибка скачивания %s", safe_url(url))
        finally:
            if close_after:
                await session.close()
        return None

    async def get_video_url(
        self, chat_id: Any, video_id: Any, message_id: Any, token: Any
    ) -> str | None:
        resp = await self.cmd(
            OpCode.GET_VIDEO_URL,
            {
                "chatId": chat_id,
                "videoId": video_id,
                "messageId": message_id,
                "token": token,
            },
        )
        for quality in ("MP4_1080", "MP4_720", "MP4_480", "MP4_360", "MP4_240"):
            if resp.get(quality):
                return resp[quality]
        return None

    async def get_file_url(
        self, chat_id: Any, file_id: Any, message_id: Any
    ) -> str | None:
        resp = await self.cmd(
            OpCode.GET_FILE_URL,
            {"chatId": chat_id, "fileId": file_id, "messageId": message_id},
        )
        return resp.get("url")

    async def send_message(self, chat_id: Any, text: str) -> dict:
        """Отправка текста в чат MAX от имени пользователя (для обратных ответов)."""
        cid = int(time.time() * 1000) * 1000 + random.randint(0, 999)
        return await self.cmd(
            OpCode.SEND_MESSAGE,
            {
                "chatId": chat_id,
                "message": {"text": text, "cid": cid, "elements": []},
                "notify": True,
            },
        )

    async def close(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    @staticmethod
    def _mask_sensitive(text: str) -> str:
        masked = re.sub(
            r'("token"\s*:\s*")[^"]+(")', r"\1***\2", text, flags=re.IGNORECASE
        )
        return re.sub(r"(MAX_USER_TOKEN=)[^\s]+", r"\1***", masked, flags=re.IGNORECASE)
