"""Управление мостом через команды в Telegram.

Пользователь (владелец) пишет боту Telegram в личный чат или в чат-приёмник:

 * ``/chats`` — список всех чатов MAX, которые бот накопил (id + название).
   Именно так узнают id чатов, поскольку API MAX больше не отдаёт список чатов.
 * ``/forward <chat_id>`` — включить пересылку из чата MAX.
 * ``/unforward <chat_id>`` — выключить пересылку из чата MAX.
 * ``/status`` — текущие настройки и счётчики.
 * ``/help`` — справка.

Команды доступны только отправителям из ``ALLOWED_TG_USER_IDS`` (если список
пустой — доступен владельца: первый, кто напишет ``/chats``... нет, просто все;
для личной установки это нормально, но лучше задать ALLOWED_TG_USER_IDS).
"""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .config import Settings
from .store import MessageStore
from .tg_sender import TelegramSender, escape_html

logger = logging.getLogger(__name__)


class BridgeCommands:
    """Живой реестр чатов для пересылки (дополняет настройки из .env)."""

    def __init__(self, settings: Settings, store: MessageStore) -> None:
        self.settings = settings
        self.store = store
        # Изменённые в рантайме наборы (поверх MAX_FORWARD_CHATS из env).
        self.extra_forward: set[str] = set(settings.forward_chats)
        self.disabled: set[str] = set()

    def is_allowed(self, user_id: int | None) -> bool:
        if not self.settings.allowed_tg_user_ids:
            return True  # личная установка: по умолчанию доверяем всем в приватных чатах
        return user_id in self.settings.allowed_tg_user_ids

    def _apply(self) -> None:
        # Пересобираем settings.forward_chats — слушатель читает его при каждом событии.
        self.settings.forward_chats = frozenset(self.extra_forward - self.disabled)

    async def cmd_chats(self, message: Message) -> None:
        chats = await self.store.list_chats()
        if not chats:
            await message.answer(
                "Пока не вижу ни одного чата MAX.<br><br>"
                "Добавьте бота MAX в нужный групповой чат или напишите ему в личные — "
                "чат появится здесь автоматически."
            )
            return
        lines = ["<b>Чаты MAX:</b>"]
        for chat_id, title, kind, last_seen in chats:
            mark = "✅" if self._enabled(chat_id) else "❌"
            name = escape_html(title) if title else "<i>без названия</i>"
            lines.append(f"{mark} <code>{chat_id}</code> [{kind}] — {name}")
        lines.append("")
        lines.append("Включить/выключить пересылку: <code>/forward &lt;id&gt;</code>, <code>/unforward &lt;id&gt;</code>")
        await message.answer("\n".join(lines))

    def _enabled(self, chat_id: str) -> bool:
        s = self.settings
        return (
            chat_id in s.forward_chats
            or (s.chat_map and chat_id in s.chat_map)
            or s.forward_all_chats
        )

    async def cmd_forward(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: <code>/forward &lt;chat_id&gt;</code> (список id — /chats)")
            return
        chat_id = command.args.split()[0].strip()
        self.extra_forward.add(chat_id)
        self.disabled.discard(chat_id)
        self._apply()
        await message.answer(f"✅ Пересылка из чата MAX <code>{escape_html(chat_id)}</code> включена.")

    async def cmd_unforward(self, message: Message, command: CommandObject) -> None:
        if not command.args:
            await message.answer("Использование: <code>/unforward &lt;chat_id&gt;</code>")
            return
        chat_id = command.args.split()[0].strip()
        self.disabled.add(chat_id)
        self.extra_forward.discard(chat_id)
        self._apply()
        await message.answer(f"❌ Пересылка из чата MAX <code>{escape_html(chat_id)}</code> выключена.")

    async def cmd_status(self, message: Message) -> None:
        forwarded = await self.store.count()
        known = await self.store.known_chat_ids()
        s = self.settings
        lines = [
            "<b>Статус моста MAX → Telegram</b>",
            f"Режим: {'пересылать ВСЕ чаты' if s.forward_all_chats else 'выборочная пересылка'}",
            f"Чатов MAX известно: {len(known)}",
            f"Сообщений переслано: {forwarded}",
            f"Чат по умолчанию: <code>{s.default_tg_chat if s.default_tg_chat is not None else '—'}</code>",
            f"Маппинги CHAT_MAP: {len(s.chat_map)}",
            f"Ручные /forward: {', '.join(sorted(self.settings.forward_chats)) or '—'}",
        ]
        await message.answer("\n".join(lines))

    async def cmd_help(self, message: Message) -> None:
        await message.answer(
            "<b>Команды моста MAX → Telegram</b>\n"
            "/chats — список чатов MAX (их id)\n"
            "/forward &lt;id&gt; — включить пересылку из чата\n"
            "/unforward &lt;id&gt; — выключить\n"
            "/status — состояние моста\n"
            "/help — эта справка\n\n"
            "Личные сообщения в MAX дублируются всегда."
        )


def build_tg_dispatcher(bridge: BridgeCommands) -> Dispatcher:
    """Создаёт диспетчер aiogram с командами управления."""
    dp = Dispatcher()

    def _guard(handler):
        async def wrapper(message: Message, command: CommandObject | None = None) -> None:
            if not bridge.is_allowed(message.from_user.id if message.from_user else None):
                await message.answer("⛔️ Доступ запрещён (ALLOWED_TG_USER_IDS).")
                return
            if command is not None:
                await handler(message, command)
            else:
                await handler(message)

        return wrapper

    dp.message.register(_guard(bridge.cmd_chats), Command("chats"), F.private)
    dp.message.register(_guard(bridge.cmd_forward), Command("forward"), F.private)
    dp.message.register(_guard(bridge.cmd_unforward), Command("unforward"), F.private)
    dp.message.register(_guard(bridge.cmd_status), Command("status"), F.private)
    dp.message.register(_guard(bridge.cmd_help), Command("help"), F.private)
    return dp


async def run_tg_control(sender: TelegramSender, dp: Dispatcher) -> None:
    """Запускает long-polling Telegram-бота (команды управления мостом)."""
    from aiogram.types import BotCommand

    try:
        await sender.bot.set_my_commands(
            [
                BotCommand(command="chats", description="Список чатов MAX и их id"),
                BotCommand(command="forward", description="Включить пересылку из чата MAX <id>"),
                BotCommand(command="unforward", description="Выключить пересылку из чата MAX <id>"),
                BotCommand(command="status", description="Состояние моста"),
                BotCommand(command="help", description="Справка"),
            ]
        )
        await dp.start_polling(sender.bot, allowed_updates=["message"])
    finally:
        pass
