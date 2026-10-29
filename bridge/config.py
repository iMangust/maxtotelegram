"""Загрузка конфигурации моста из переменных окружения / .env файла."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


class ConfigError(RuntimeError):
    """Ошибка конфигурации приложения."""


def _parse_chat_map(raw: str) -> dict[str, int]:
    """Разбирает строку вида ``maxChatId:tgChatId,maxChatId:tgChatId``."""
    mapping: dict[str, int] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if ":" not in pair:
            raise ConfigError(
                f"Некорректная пара маппинга '{pair}', ожидается формат 'chatIdMax:chatIdTelegram'"
            )
        max_id, tg_id = (part.strip() for part in pair.split(":", 1))
        try:
            mapping[max_id] = int(tg_id)
        except ValueError as exc:
            raise ConfigError(f"Telegram chat id '{tg_id}' должен быть числом") from exc
    return mapping


@dataclass(slots=True)
class Settings:
    """Настройки моста MAX -> Telegram."""

    max_token: str
    tg_token: str
    # Режим источника MAX: "bot" (официальный Bot API) или "user"
    # (неофициальный WebSocket личного аккаунта — видит все личные диалоги).
    mode: str = "bot"
    max_user_token: str = ""
    max_device_id: str = ""
    # Белый список чатов для user-режима (MAX_CHAT_IDS); пусто = все.
    user_chat_ids: frozenset[str] = frozenset()
    # Чаты, исключённые из пересылки в user-режиме (MAX_EXCLUDE_CHAT_IDS).
    exclude_chats: frozenset[str] = frozenset()
    # Пустой словарь маппинга означает "пересылать сообщения из всех чатов
    # в чат по умолчанию TG_CHAT_ID".
    chat_map: dict[str, int] = field(default_factory=dict)
    default_tg_chat: int | None = None
    # Разрешённые отправители для реверсивного направления (пусто = все).
    allowed_tg_user_ids: tuple[int, ...] = ()
    # Личный Telegram-чат владельца (для команд /chats, /status и уведомлений).
    owner_chat_id: int | None = None
    # Личный MAX user_id владельца — его личные сообщения всегда пересылаются.
    max_owner_user_id: int | None = None
    # Пересылать ВСЕ групповые чаты/каналы в default_tg_chat (личный режим).
    forward_all_chats: bool = False
    # Ручной список чатов MAX для пересылки ("123,456"); дополняет CHAT_MAP.
    forward_chats: frozenset[str] = frozenset()
    # Отправлять в Telegram служебные команды (/start и т.п.).
    forward_commands: bool = False
    # Тег для закреплённого сообщения с таблицей соответствия сообщений.
    pinned_tag: str = ""
    debug: bool = False

    def telegram_target(self, max_chat_id: str | int) -> int | None:
        """Возвращает чат Telegram, куда нужно переслать сообщение из чата MAX."""
        key = str(max_chat_id)
        if self.chat_map:
            target = self.chat_map.get(key)
            if target is not None:
                return target
        return self.default_tg_chat

    @property
    def listens_to_max_chats(self) -> set[str] | None:
        """None = слушать все чаты, иначе — только перечисленные."""
        if self.chat_map and self.default_tg_chat is None:
            return set(self.chat_map.keys())
        return None

    def notify_chat(self) -> int | None:
        """Чат Telegram, куда слать служебные уведомления (команды /chats и т.п.)."""
        return self.owner_chat_id or self.default_tg_chat


def load_settings(env_file: str | None = None) -> Settings:
    """Читает настройки из окружения (и опционального .env файла)."""
    load_dotenv(env_file or os.environ.get("BRIDGE_DOTENV", ".env"))

    def required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ConfigError(f"Переменная окружения {name} не задана")
        return value

    def optional(name: str) -> str:
        return os.getenv(name, "").strip()

    def optional_int(name: str) -> int | None:
        value = os.getenv(name, "").strip()
        if not value:
            return None
        try:
            return int(value)
        except ValueError as exc:
            raise ConfigError(f"{name} должно быть числом, получено: {value!r}") from exc

    def optional_bool(name: str, default: bool = False) -> bool:
        value = os.getenv(name, "").strip().lower()
        if not value:
            return default
        return value in {"1", "true", "yes", "on"}

    mode = optional("MAX_MODE").lower() or "auto"
    max_token = optional("MAX_BOT_TOKEN")
    max_user_token = optional("MAX_USER_TOKEN")
    max_device_id = optional("MAX_DEVICE_ID")
    if mode == "auto":
        mode = "user" if (max_user_token and max_device_id) else "bot"
    if mode not in {"bot", "user"}:
        raise ConfigError(f"MAX_MODE должен быть bot или user, получено: {mode!r}")
    if mode == "bot" and not max_token:
        raise ConfigError("Для режима bot нужна переменная MAX_BOT_TOKEN")
    if mode == "user" and not (max_user_token and max_device_id):
        raise ConfigError(
            "Для режима user нужны MAX_USER_TOKEN и MAX_DEVICE_ID "
            "(см. README: Local Storage на web.max.ru)"
        )

    def id_list(name: str) -> frozenset[str]:
        raw = optional(name)
        return frozenset(x.strip() for x in raw.split(",") if x.strip()) if raw else frozenset()

    chat_map_raw = os.getenv("CHAT_MAP", "").strip()
    chat_map = _parse_chat_map(chat_map_raw) if chat_map_raw else {}
    default_tg_chat = optional_int("TG_CHAT_ID")

    if not chat_map and default_tg_chat is None:
        raise ConfigError("Не задан целевой чат Telegram: укажите TG_CHAT_ID или CHAT_MAP")

    allowed_raw = os.getenv("ALLOWED_TG_USER_IDS", "").strip()
    allowed = tuple(int(x.strip()) for x in allowed_raw.split(",") if x.strip()) if allowed_raw else ()

    forward_chats_raw = os.getenv("MAX_FORWARD_CHATS", "").strip()
    forward_chats = frozenset(
        x.strip() for x in forward_chats_raw.split(",") if x.strip()
    ) if forward_chats_raw else frozenset()

    return Settings(
        max_token=max_token,
        mode=mode,
        max_user_token=max_user_token,
        max_device_id=max_device_id,
        user_chat_ids=id_list("MAX_CHAT_IDS"),
        exclude_chats=id_list("MAX_EXCLUDE_CHAT_IDS"),
        tg_token=required("TG_BOT_TOKEN"),
        chat_map=chat_map,
        default_tg_chat=default_tg_chat,
        allowed_tg_user_ids=allowed,
        owner_chat_id=optional_int("TG_OWNER_CHAT_ID"),
        max_owner_user_id=optional_int("MAX_OWNER_USER_ID"),
        forward_all_chats=optional_bool("FORWARD_ALL_CHATS"),
        forward_chats=forward_chats,
        forward_commands=optional_bool("FORWARD_COMMANDS"),
        pinned_tag=os.getenv("PINNED_TAG", "").strip(),
        debug=optional_bool("DEBUG"),
    )
