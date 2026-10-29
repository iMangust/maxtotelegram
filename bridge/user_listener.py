"""Слушатель MAX в режиме пользователя (userbot).

Видит ВСЕ личные диалоги и чаты вашего аккаунта — бота никуда добавлять не
нужно, новые сообщения от незнакомцев пересылаются автоматически.

Формирует заголовок «✉ Имя» для личных диалогов и «💬 Чат | Имя» для групп,
скачивает вложения (фото/видео/файлы/аудио/стикеры) через RPC клиента и
отправляет их в Telegram через существующий ``TelegramSender``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from typing import Any

from .convert import DownloadedAttachment
from .store import MessageStore
from .tg_sender import TelegramSender, build_header, escape_html
from .user_client import MaxUserClient, OpCode, UserMessage

logger = logging.getLogger(__name__)

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}

_SKIP_TYPES = {"CONTROL", "WIDGET", "INLINE_KEYBOARD", None}


def _guess_media_kind(filename: str) -> str:
    name_lower = (filename or "").lower()
    for ext in PHOTO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "photo"
    for ext in VIDEO_EXTENSIONS:
        if name_lower.endswith(ext):
            return "video"
    return "doc"


def _human_size(n: int) -> str:
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "Б" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def is_dm(client: MaxUserClient, chat_id: Any) -> bool:
    return client.chat_types.get(chat_id) == "DIALOG"


def should_forward_user(
    client: MaxUserClient, msg: UserMessage, exclude: set[str], include: set[str]
) -> bool:
    """Решает, пересылать ли сообщение в user-режиме.

    * свои сообщения — никогда;
    * чаты из MAX_EXCLUDE_CHAT_IDS — никогда;
    * если задан белый список MAX_CHAT_IDS — только он;
    * иначе — всё остальное (личный сценарий «дублировать все»).
    """
    if msg.is_self:
        return False
    cid = str(msg.chat_id)
    if cid in exclude:
        return False
    if include and cid not in include:
        return False
    return True


async def _prepare_attach(
    client: MaxUserClient, attach: dict, chat_id: Any, message_id: Any
) -> DownloadedAttachment | None:
    """Возвращает скачанные байты вложения или None (заглушка в тексте)."""
    atype = attach.get("_type", "")

    if atype == "PHOTO":
        url = attach.get("baseUrl") or attach.get("url")
        if not url:
            return None
        data = await client.download_file(url)
        return DownloadedAttachment(data=data, filename="photo.jpg", kind="photo") if data else None

    if atype == "VIDEO":
        video_id = attach.get("videoId")
        token = attach.get("token")
        if video_id and chat_id and message_id:
            url = await client.get_video_url(chat_id, video_id, message_id, token)
            if url:
                data = await client.download_file(url, omit_origin=True)
                if data:
                    return DownloadedAttachment(
                        data=data, filename="video.mp4", kind="video"
                    )
        thumb = attach.get("thumbnail")
        if thumb:
            data = await client.download_file(thumb)
            if data:
                return DownloadedAttachment(
                    data=data, filename="video_preview.jpg", kind="photo"
                )
        return None

    if atype == "FILE":
        name = attach.get("name", "file")
        url = attach.get("url")
        file_id = attach.get("fileId")
        if not (url and str(url).startswith("http")) and file_id and chat_id and message_id:
            url = await client.get_file_url(chat_id, file_id, message_id)
        if url:
            data = await client.download_file(url)
            if data:
                return DownloadedAttachment(
                    data=data, filename=name, kind=_guess_media_kind(name)
                )
        return None

    if atype in ("AUDIO", "STICKER"):
        url = attach.get("url")
        if url:
            data = await client.download_file(url)
            kind = "photo" if atype == "STICKER" else "doc"
            if data:
                return DownloadedAttachment(
                    data=data,
                    filename="sticker.png" if atype == "STICKER" else "audio.opus",
                    kind=kind,
                )
        return None

    # LOCATION / CONTACT / POLL / SHARE — текстовые заглушки
    return None


def _attach_stub(attach: dict) -> str:
    atype = attach.get("_type", "unknown")
    if atype == "LOCATION":
        lat = attach.get("lat") or attach.get("latitude")
        lon = attach.get("lon") or attach.get("lng") or attach.get("longitude")
        return f"📍 {lat}, {lon}" if lat and lon else "<i>[геолокация]</i>"
    if atype == "CONTACT":
        name = escape_html(str(attach.get("name", "")))
        phone = escape_html(str(attach.get("phone", "")))
        return f"👤 {name}" + (f" — {phone}" if phone else "")
    if atype == "POLL":
        return f"📊 <b>{escape_html(str(attach.get('title', 'Опрос')))}</b>"
    if atype == "FILE":
        name = escape_html(str(attach.get("name", "file")))
        size = attach.get("size", 0)
        extra = f" ({_human_size(size)})" if size else ""
        return f"📎 <b>{name}</b>{extra} <i>[не удалось скачать]</i>"
    return f"<i>[вложение: {escape_html(str(atype or 'unknown'))}]</i>"


def build_user_listener(
    settings: Any,
    sender: TelegramSender,
    store: MessageStore,
    client: MaxUserClient,
) -> MaxUserClient:
    """Настраивает колбэки клиента на пересылку сообщений в Telegram."""
    default_target = settings.default_tg_chat or settings.owner_chat_id
    exclude = {str(x) for x in getattr(settings, "exclude_chats", ())}
    include = {str(x) for x in getattr(settings, "user_chat_ids", ())}

    @client.on_ready
    async def handle_ready(snapshot: dict, reconnecting: bool = False) -> None:
        count = len(client.chats)
        if default_target is not None:
            status = "переподключён" if reconnecting else "подключён"
            await sender.send_plain(
                default_target,
                f"✅ <b>MAX (user-режим)</b>: {status} | диалогов: {count}",
            )
        logger.info("MAX user: известно чатов: %s", client.chats)

    @client.on_disconnect
    async def handle_disconnect() -> None:
        if default_target is not None:
            await sender.send_plain(
                default_target, "⚠️ <b>MAX</b>: соединение потеряно, переподключение..."
            )

    @client.on_message
    async def handle_message(msg: UserMessage) -> None:
        if not should_forward_user(client, msg, exclude, include):
            return
        if not msg.message_id:
            return
        if await store.get_tg_by_max(f"user:{msg.message_id}"):
            return  # повторная доставка

        sender_label = escape_html(await client.resolve_user(msg.sender_id))
        dm = is_dm(client, msg.chat_id)
        chat_title = "" if dm or len(include) == 1 else str(
            client.chats.get(msg.chat_id, "") or ""
        )
        if dm and not chat_title:
            # для подписи в заголовке группы не нужен
            pass
        header = build_header(sender_label, chat_title or None)

        await store.remember_chat(
            str(msg.chat_id),
            title=str(client.chats.get(msg.chat_id, "") or ""),
            kind="dialog" if dm else "chat",
        )

        meaningful = [
            a
            for a in msg.attaches
            if isinstance(a, dict) and a.get("_type") not in _SKIP_TYPES
        ]

        target = settings.telegram_target(str(msg.chat_id)) or default_target
        if target is None:
            logger.warning("MAX user: нет целевого чата Telegram для %s", msg.chat_id)
            return

        text_html = html.escape(msg.text or "", quote=False)

        if not meaningful:
            if not text_html.strip():
                return
            tg_id = await sender.send_forwarded(target, header, text_html, [])
        else:
            downloaded: list[DownloadedAttachment] = []
            stubs: list[str] = []
            for attach in meaningful:
                prepared = await _prepare_attach(
                    client, attach, msg.chat_id, msg.message_id
                )
                if prepared is not None:
                    downloaded.append(prepared)
                else:
                    stubs.append(_attach_stub(attach))
            if stubs:
                text_html = (text_html + "\n" if text_html else "") + "\n".join(stubs)
            if not downloaded and not text_html.strip():
                return
            tg_id = await sender.send_forwarded(
                target, header, text_html, downloaded
            )

        if tg_id is not None:
            await store.remember(f"user:{msg.message_id}", tg_id, target)
            logger.info(
                "MAX user: переслано chat=%s -> TG %s/%s", msg.chat_id, target, tg_id
            )

    return client


async def run_max_user_listener(
    settings: Any, sender: TelegramSender, store: MessageStore
) -> None:
    """Запускает user-режим до отмены задачи."""
    client = MaxUserClient(
        token=settings.max_user_token, device_id=settings.max_device_id
    )
    build_user_listener(settings, sender, store, client)
    try:
        await client.run()
    finally:
        await client.close()
