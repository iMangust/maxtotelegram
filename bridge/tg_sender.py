"""Отправка сообщений в Telegram через Bot API (aiogram).

Все исходящие сообщения помечаются тегом ``#forwarded_from_max``, чтобы:
 * читатель понимал происхождение сообщения;
 * бот мог отличить собственные пересылки от ответов пользователей
   (защита от бесконечного эха при включённом обратном направлении).
"""

from __future__ import annotations

import io
import logging
from typing import Any

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from .convert import DownloadedAttachment

logger = logging.getLogger(__name__)

FORWARD_TAG = "#forwarded_from_max"
_MAX_CAPTION_LEN = 1024


def build_header(author_name: str, chat_title: str | None = None) -> str:
    """Строит заголовок пересланного сообщения."""
    parts = [f"<b>{escape_html(author_name)}</b>"]
    if chat_title:
        parts.append(f"<i>{escape_html(chat_title)}</i>")
    return " | ".join(parts)


def escape_html(text: str) -> str:
    import html as _html

    return _html.escape(text, quote=False)


class TelegramSender:
    """Обёртка над aiogram.Bot для пересылки сообщений из MAX."""

    def __init__(self, token: str) -> None:
        self._token = token  # приватно: не попадает в repr/логи
        self.bot = Bot(
            token=token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )

    def __repr__(self) -> str:  # защита от случайного дампа секрета
        return "<TelegramSender token=***>"

    async def close(self) -> None:
        await self.bot.session.close()

    async def send_plain(self, chat_id: int, text_html: str) -> int | None:
        """Служебное сообщение без тега пересылки (ответ на команду, уведомление)."""
        try:
            sent = await self.bot.send_message(chat_id=chat_id, text=text_html)
            return sent.message_id
        except Exception as exc:  # noqa: BLE001
            logger.error("Telegram: не удалось отправить служебное сообщение в %s: %s", chat_id, exc)
            return None

    async def send_forwarded(
        self,
        chat_id: int,
        header: str,
        text_html: str,
        attachments: list[DownloadedAttachment] | None = None,
    ) -> int | None:
        """Отправляет пересобранное сообщение из MAX в Telegram.

        Возвращает message_id отправленного сообщения (или None при неудаче).
        Если есть вложения — текст уходит подписью к первому файлу, остальные
        файлы отправляются отдельными сообщениями.
        """
        caption_body = text_html[:_MAX_CAPTION_LEN] if text_html else ""
        caption = f"{header}\n{caption_body}" if caption_body else header
        full_footer = f"\n\n{FORWARD_TAG}"
        attachments = attachments or []

        try:
            if attachments:
                first = attachments[0]
                sent = await self._send_file(chat_id, first, caption + full_footer)
                # Остальные файлы — без текста.
                for extra in attachments[1:]:
                    await self._send_file(chat_id, extra, FORWARD_TAG)
                return sent.message_id if sent else None

            body = f"{header}\n{text_html}{full_footer}" if text_html else f"{header}{full_footer}"
            sent = await self.bot.send_message(chat_id=chat_id, text=body)
            return sent.message_id
        except TelegramForbiddenError:
            logger.error("Telegram: нет доступа к чату %s (бот удалён/забанен)", chat_id)
        except TelegramBadRequest as exc:
            logger.error("Telegram: ошибка запроса в чат %s: %s", chat_id, exc)
        except Exception as exc:  # noqa: BLE001 - сеть/лимиты, логируем и идём дальше
            logger.exception("Telegram: неизвестная ошибка отправки: %s", exc)
        return None

    async def _send_file(
        self, chat_id: int, attachment: DownloadedAttachment, caption: str
    ) -> Any:
        file_obj = io.BytesIO(attachment.data)
        file_obj.name = attachment.filename or "attachment"
        kwargs = {"chat_id": chat_id, "caption": caption}
        if attachment.kind == "photo":
            return await self.bot.send_photo(photo=file_obj, **kwargs)
        if attachment.kind == "video":
            return await self.bot.send_video(video=file_obj, **kwargs)
        return await self.bot.send_document(document=file_obj, **kwargs)
