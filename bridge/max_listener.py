"""Слушатель MAX: подписывается на новые сообщения и пересылает их в Telegram.

Используется библиотека ``maxapi`` (официальный Bot API мессенджера MAX,
``https://maxapi.ru`` / ``https://dev.max.ru``). Бот должен состоять в чатах,
сообщения из которых нужно пересылать.
"""

from __future__ import annotations

import logging

from maxapi import Bot, Dispatcher, Router
from maxapi.filters import BaseFilter
from maxapi.types import MessageCreated

from .config import Settings
from .convert import convert_text_to_html, download_attachments
from .store import MessageStore
from .tg_sender import TelegramSender, build_header

logger = logging.getLogger(__name__)


class NotBotMessage(BaseFilter):
    """Пропускает только сообщения, написанные людьми (не ботами)."""

    async def __call__(self, event: MessageCreated) -> bool:
        user = getattr(event, "from_user", None)
        return not (user is not None and bool(getattr(user, "is_bot", False)))


def _chat_id_of(event: MessageCreated) -> str | None:
    """Возвращает id чата-получателя сообщения.

    В maxapi 1.x у события нет отдельного поля ``chat`` до обогащения, а
    также оно может остаться ``None`` при ошибке API. Надёжный источник —
    ``message.recipient.chat_id`` (экземпляр ``Recipient``, а не словарь).
    """
    message = getattr(event, "message", None)
    recipient = getattr(message, "recipient", None)
    chat_id = getattr(recipient, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(event, "chat", None), "chat_id", None)
    return str(chat_id) if chat_id is not None else None


def _author_name(event: MessageCreated) -> str:
    # Сначала message.sender (реальный отправитель), затем обогащённый from_user.
    user = getattr(event.message, "sender", None) if getattr(event, "message", None) else None
    if user is None:
        user = getattr(event, "from_user", None)
    if user is None:
        return "Неизвестный"
    first = getattr(user, "first_name", "") or ""
    last = getattr(user, "last_name", "") or ""
    name = f"{first} {last}".strip()
    username = getattr(user, "username", "") or ""
    if username:
        name = f"{name} (@{username})" if name else f"@{username}"
    return name or "Неизвестный"


def build_max_router(
    settings: Settings,
    sender: TelegramSender,
    store: MessageStore,
    bot: Bot | None = None,
) -> Router:
    """Создаёт роутер maxapi с обработчиком новых сообщений.

    :param bot: экземпляр ``Bot`` для скачивания вложений (нужен для
        тестов; в рабочем режиме берётся из обогащённого события).
    """
    router = Router()

    @router.message_created(NotBotMessage())
    async def on_message_created(event: MessageCreated) -> None:
        message = event.message
        if message is None or message.body is None:
            return

        chat_id = _chat_id_of(event)
        if chat_id is None:
            return

        target = settings.telegram_target(chat_id)
        if target is None:
            logger.debug("Чат MAX %s не настроен для пересылки — пропускаем", chat_id)
            return

        mid = str(getattr(message.body, "mid", "") or "")
        if not mid:
            return
        if await store.get_tg_by_max(mid):
            # Уже пересылали это сообщение (повторная доставка update).
            return

        text_html = convert_text_to_html(message.body.text or "")
        attachments = await download_attachments(getattr(event, "bot", None) or bot, message.body.attachments)

        if not text_html.strip() and not attachments:
            return

        header = build_header(_author_name(event), getattr(getattr(event, "chat", None), "title", None))
        tg_msg_id = await sender.send_forwarded(target, header, text_html, attachments)
        if tg_msg_id is not None:
            await store.remember(mid, tg_msg_id, target)
            logger.info(
                "Переслано MAX:%s -> TG:%s/%s", chat_id, target, tg_msg_id
            )

    return router


async def run_max_listener(settings: Settings, sender: TelegramSender, store: MessageStore) -> None:
    """Запускает long-polling бота MAX до отмены."""
    bot = Bot(token=settings.max_token)
    dp = Dispatcher()
    dp.include_router(build_max_router(settings, sender, store, bot=bot))

    me = await bot.get_me()
    logger.info("Бот MAX запущен: @%s (id=%s)", getattr(me, "username", "?"), getattr(me, "user_id", "?"))

    try:
        await dp.start_polling(bot)
    finally:
        await bot.close_session()
