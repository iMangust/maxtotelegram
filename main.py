"""Точка входа моста MAX -> Telegram.

Запуск:
    export MAX_BOT_TOKEN=... TG_BOT_TOKEN=... TG_CHAT_ID=...
    python main.py

Конфигурация читается из переменных окружения и (опционально) .env файла;
подробности — в ``bridge/config.py`` и ``.env.example``.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from bridge.config import ConfigError, load_settings
from bridge.max_listener import run_max_listener
from bridge.store import MessageStore
from bridge.tg_commands import BridgeCommands, build_tg_dispatcher, run_tg_control
from bridge.tg_sender import TelegramSender
from bridge.user_listener import run_max_user_listener

logger = logging.getLogger("bridge")


async def main() -> None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        logger.error("Ошибка конфигурации: %s", exc)
        raise SystemExit(2) from exc

    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    store = MessageStore()
    await store.connect()
    sender = TelegramSender(settings.tg_token)

    bridge = BridgeCommands(settings, store)
    tg_dp = build_tg_dispatcher(bridge)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows
            pass

    if settings.mode == "user":
        logger.info("Режим MAX: user (личный аккаунт, все диалоги)")
        max_task = asyncio.create_task(
            run_max_user_listener(settings, sender, store), name="max-listener"
        )
    else:
        logger.info("Режим MAX: bot (официальный Bot API)")
        max_task = asyncio.create_task(
            run_max_listener(settings, sender, store), name="max-listener"
        )
    control = asyncio.create_task(
        run_tg_control(sender, tg_dp), name="tg-control"
    )

    async def waiter() -> None:
        await stop_event.wait()
        max_task.cancel()
        control.cancel()

    try:
        await asyncio.gather(max_task, control, waiter(), return_exceptions=True)
    finally:
        await sender.close()
        await store.close()
        logger.info("Мост остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover
        pass
