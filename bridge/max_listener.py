"""Слушатель MAX: подписывается на новые сообщения и пересылает их в Telegram.

Используется библиотека ``maxapi`` (Bot API мессенджера MAX,
``https://dev.max.ru``).

Важное ограничение Bot API MAX (актуально с июня 2026 года): метод
``GET /chats`` не поддерживается — бот не может запросить список чатов.
Поэтому реестр чатов накапливаем сами из событий ``bot_added``,
``bot_started``, ``message_created`` и удаляем записи по ``bot_removed`` /
``bot_stopped`` (см. докстринг ``maxapi.methods.get_chats``). Реестр хранится
в таблице ``known_chats`` (``bridge/store.py``), а команда ``/chats`` в
Telegram показывает накопленный список с id чатов.

Как бот понимает, ЧТО пересылать (сценарий личного дублирования):
 * личные сообщения вам боту (chat_type=DIALOG) пересылаются всегда;
 * групповые чаты/каналы пересылаются, если:
     - задан ``CHAT_MAP`` с конкретным чатом, ИЛИ
     - включён ``FORWARD_ALL_CHATS=true`` (пересылать всё в ``TG_CHAT_ID``),
       ИЛИ чат помечен вручную через ``MAX_FORWARD_CHATS`` / команду ``/forward``;
 * собственные сообщения бота и служебные события игнорируются.
"""

from __future__ import annotations

import logging

from maxapi import Bot, Dispatcher, Router
from maxapi.filters import BaseFilter
from maxapi.types import BotAdded, BotRemoved, BotStarted, BotStopped, MessageCreated

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

    Надёжный источник — ``message.recipient.chat_id`` (экземпляр
    ``Recipient`` с обязательными ``chat_id`` и ``chat_type``).
    """
    message = getattr(event, "message", None)
    recipient = getattr(message, "recipient", None)
    chat_id = getattr(recipient, "chat_id", None)
    if chat_id is None:
        chat_id = getattr(getattr(event, "chat", None), "chat_id", None)
    return str(chat_id) if chat_id is not None else None


def _chat_type_of(event: MessageCreated) -> str:
    recipient = getattr(getattr(event, "message", None), "recipient", None)
    return str(getattr(recipient, "chat_type", "chat") or "chat").lower()


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


def should_forward(settings: Settings, chat_id: str, chat_type: str) -> bool:
    """Решает, пересылать ли сообщение из этого чата."""
    if chat_type == "dialog":
        # Личные сообщения — всегда дублируем владельцу.
        return True
    # Явный маппинг CHAT_MAP имеет наивысший приоритет (в т.ч. в тестах).
    if settings.chat_map and str(chat_id) in settings.chat_map:
        return True
    if str(chat_id) in settings.forward_chats:
        return True
    if settings.forward_all_chats:
        return True
    # Личный режим: задан только чат по умолчанию без ограничений — шлём всё.
    if not settings.chat_map and settings.default_tg_chat is not None:
        return True
    return False


def build_max_router(
    settings: Settings,
    sender: TelegramSender,
    store: MessageStore,
    bot: Bot | None = None,
) -> Router:
    """Создаёт роутер maxapi с обработчиками сообщений и событий чатов.

    :param bot: экземпляр ``Bot`` для скачивания вложений (нужен для
        тестов; в рабочем режиме берётся из обогащённого события).
    """
    router = Router()

    async def _notify(text: str) -> None:
        chat = settings.notify_chat()
        if chat is not None:
            await sender.send_plain(chat, text)

    # --- Новые сообщения -----------------------------------------------------

    @router.message_created(NotBotMessage())
    async def on_message_created(event: MessageCreated) -> None:
        message = event.message
        if message is None or message.body is None:
            return

        chat_id = _chat_id_of(event)
        if chat_id is None:
            return
        chat_type = _chat_type_of(event)

        # Накапливаем реестр чатов (замены удалённому GET /chats нет).
        title = getattr(getattr(event, "chat", None), "title", None)
        await store.remember_chat(chat_id, title=title, kind=chat_type)

        if not should_forward(settings, chat_id, chat_type):
            logger.debug("Чат MAX %s (%s) не настроен для пересылки", chat_id, chat_type)
            return

        target = settings.telegram_target(chat_id)
        if target is None:
            return

        mid = str(getattr(message.body, "mid", "") or "")
        if not mid:
            return
        if await store.get_tg_by_max(mid):
            # Уже пересылали это сообщение (повторная доставка update).
            return

        text_html = convert_text_to_html(message.body.text or "")
        attachments = await download_attachments(
            getattr(event, "bot", None) or bot, message.body.attachments
        )

        if not text_html.strip() and not attachments:
            return

        header = build_header(_author_name(event), title)
        tg_msg_id = await sender.send_forwarded(target, header, text_html, attachments)
        if tg_msg_id is not None:
            await store.remember(mid, tg_msg_id, target)
            logger.info("Переслано MAX:%s -> TG:%s/%s", chat_id, target, tg_msg_id)

    # --- События membership: ведём реестр чатов ------------------------------

    @router.bot_added()
    async def on_bot_added(event: BotAdded) -> None:
        kind = "channel" if getattr(event, "is_channel", False) else "chat"
        await store.remember_chat(event.chat_id, kind=kind)
        logger.info("Бот добавлен в чат %s (%s)", event.chat_id, kind)
        await _notify(
            "🆕 Бот добавлен в чат MAX <code>{}</code> ({}).\n"
            "Чтобы включить пересылку: /forward {}".format(
                event.chat_id, kind, event.chat_id
            )
        )

    @router.bot_started()
    async def on_bot_started(event: BotStarted) -> None:
        await store.remember_chat(event.chat_id, kind="dialog")
        logger.info("Бот запущен пользователем в диалоге %s", event.chat_id)

    @router.bot_removed()
    async def on_bot_removed(event: BotRemoved) -> None:
        await store.forget_chat(event.chat_id)
        logger.info("Бот удалён из чата %s", event.chat_id)
        await _notify(
            f"⛔️ Бот удалён из чата MAX <code>{event.chat_id}</code> — пересылка прекращена."
        )

    @router.bot_stopped()
    async def on_bot_stopped(event: BotStopped) -> None:
        await store.forget_chat(event.chat_id)
        logger.info("Бот остановлен в диалоге %s", event.chat_id)

    return router


async def run_max_listener(settings: Settings, sender: TelegramSender, store: MessageStore) -> None:
    """Запускает long-polling бота MAX до отмены."""
    bot = Bot(token=settings.max_token)
    dp = Dispatcher()
    dp.include_router(build_max_router(settings, sender, store, bot=bot))

    me = await bot.get_me()
    logger.info(
        "Бот MAX запущен: @%s (id=%s)",
        getattr(me, "username", "?"),
        getattr(me, "user_id", "?"),
    )

    chats = await store.list_chats()
    if chats:
        logger.info("Известные чаты MAX: %s", ", ".join(c[0] for c in chats))
    else:
        logger.info(
            "Реестр чатов пуст. Добавьте бота в нужный чат MAX или напишите ему "
            "личное сообщение — чат появится автоматически. Список id: команда /chats в Telegram."
        )

    try:
        await dp.start_polling(bot)
    finally:
        await bot.close_session()
