# MAX → Telegram мост

Приложение пересылает сообщения из чатов мессенджера **MAX** (бот на
[maxapi](https://pypi.org/project/maxapi/)) в чаты **Telegram** (бот на
[aiogram](https://docs.aiogram.dev/)).

## Важное ограничение (прочитайте первым)
MAX предоставляет **только Bot API** (как Telegram Bot API). Бот получает
события исключительно из диалогов, где он состоит. Поэтому:
* личные сообщения, написанные *вам*, переслать нельзя — их не увидит ни
  один бот; дублируются только сообщения в чатах/диалогах с добавленным
  MAX-ботом;
* «новый человек, который пишет вам впервые», попадёт в Telegram только
  после того, как вы добавите бота в этот диалог.
Подробности и способы обхода ограничений — в [docs/FAQ.md](docs/FAQ.md).

## Возможности
* Пересылка текста с сохранением упоминаний (`{{Ivanov}}` → HTML) и
  экранированием;
* Пересылка вложений: изображения, видео, файлы, стикеры, аудио;
* Заголовок вида `Имя Фамилия (@username) | Название чата` + тег
  `#forwarded_from_max`;
* Маппинг чатов `MAX → Telegram` (`CHAT_MAP`) или единый целевой чат
  (`TG_CHAT_ID`);
* Защита от повторной доставки (SQLite-таблица соответствий `bridge.db`)
  и фильтрация сообщений ботов;
* Корректное завершение по SIGINT/SIGTERM.

## Установка и запуск
```bash
pip install -r requirements.txt
cp .env.example .env   # заполнить токены и TG_CHAT_ID
python main.py
```

Бот MAX должен состоять в нужных чатах; токен получается через @MasterBot
в MAX, токен Telegram — через @BotFather. Бот Telegram должен быть добавлен
в целевой чат (для супергрупп id указывается со `-100`).

## Тесты
```bash
pytest tests/
```
19 тестов покрывают валидацию типов maxapi (recipient — экземпляр
`Recipient`, `timestamp` — int, `mid` — строго str, lowercase enum
вложений) и сквозную логику обработчика через моки sender/store.

## Структура
```
main.py               точка входа (цикл событий, graceful shutdown)
bridge/config.py      настройки из env/.env, маппинг чатов
bridge/max_listener.py слушатель MAX (Router + NotBotMessage фильтр)
bridge/convert.py     конвертация текста/вложений, скачивание файлов
bridge/tg_sender.py   отправка в Telegram (aiogram Bot)
bridge/store.py       SQLite-маппинг MAX msg id -> TG msg id
```
