"""Преобразование сообщений MAX в формат, подходящий для Telegram.

Telegram-бот принимает текст в HTML-разметке (parse_mode=HTML), поэтому:
 * обычный текст экранируется;
 * markdown-разметка MAX (*жирный*, _курсив_, `код`) переводится в HTML;
 * упоминания пользователей превращаются в "<b>[Имя Фамилия]</b>";
 * вложения (фото/видео/документы) скачиваются из MAX и загружаются в Telegram.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import Any

# --- Конвертация текста ------------------------------------------------------

_MD_CODE_BLOCK = re.compile(r"```[^\S\n]*\w*\n(.*?)```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"(?<!\w)\*\*?(?=\S)(.+?)(?<=\S)\*\*?(?!\w)", re.DOTALL)
_MD_ITALIC = re.compile(r"(?<![\w`])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w`])")
_MD_STRIKE = re.compile(r"~(?=\S)([^~\n]+?)(?<=\S)~")
_MAX_MENTION = re.compile(r"\{\{(.+?)\}\}")


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def convert_text_to_html(text: str, mentions: dict[str, str] | None = None) -> str:
    """Переводит текст сообщения MAX (с markdown-разметкой) в HTML для Telegram.

    :param text: исходный текст сообщения.
    :param mentions: соответствие "id/имя в фигурных скобках -> отображаемое имя".
    """
    if not text:
        return ""

    parts: list[str] = []
    pos = 0
    # Блоки кода обрабатываем первыми, чтобы их содержимое не трогала остальная
    # разметка.
    for match in _MD_CODE_BLOCK.finditer(text):
        parts.append(_inline_to_html(text[pos : match.start()], mentions))
        parts.append(f"<pre><code>{_escape(match.group(1).rstrip())}</code></pre>")
        pos = match.end()
    parts.append(_inline_to_html(text[pos:], mentions))
    return "".join(parts)


def _inline_to_html(chunk: str, mentions: dict[str, str] | None) -> str:
    # 1. Заменяем упоминания {{...}} на плейсхолдеры, которые позже стануют
    #    готовыми HTML-фрагментами.
    placeholders: list[str] = []

    def _mention_repl(match: re.Match[str]) -> str:
        key = match.group(1)
        label = (mentions or {}).get(key) or key
        token = f"\x00MENTION{len(placeholders)}\x00"
        placeholders.append(f"<b>[{_escape(label)}]</b>")
        return token

    chunk = _MAX_MENTION.sub(_mention_repl, chunk)

    # 2. Экранируем весь текст.
    escaped = _escape(chunk)

    # 3. Превращаем markdown в HTML. Плейсхолдеры состоят из \x00 и цифр,
    #    поэтому регуляркам они не мешают.
    escaped = _MD_INLINE_CODE.sub(
        lambda m: f"<code>{m.group(1)}</code>", escaped
    )
    escaped = _MD_STRIKE.sub(lambda m: f"<s>{m.group(1)}</s>", escaped)
    escaped = _MD_BOLD.sub(lambda m: f"<b>{m.group(1)}</b>", escaped)
    escaped = _MD_ITALIC.sub(lambda m: f"<i>{m.group(1)}</i>", escaped)

    # 4. Возвращаем упоминания.
    for idx, fragment in enumerate(placeholders):
        escaped = escaped.replace(f"\x00MENTION{idx}\x00", fragment)

    return escaped


# --- Разбор вложений ----------------------------------------------------------


@dataclass(slots=True)
class DownloadedAttachment:
    """Скачанное из MAX вложение, готовое к отправке в Telegram."""

    data: bytes
    filename: str
    kind: str  # photo | video | doc


@dataclass(slots=True)
class PreparedMessage:
    """Сообщение MAX, подготовленное к отправке в Telegram."""

    text: str = ""
    attachments: list[DownloadedAttachment] = field(default_factory=list)
    skip: bool = False
    skip_reason: str = ""

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.attachments)


def attachment_kind(att_type: str) -> str:
    """Определяет тип вложения Telegram по типу вложения MAX.

    maxapi использует значения enum ``AttachmentType`` в нижнем регистре
    (``image``, ``video``, ``file``, ...), исторические варианты с
    заглавной буквы оставлены для совместимости.
    """
    mapping = {
        "image": "photo",
        "video": "video",
        "audio": "doc",
        "file": "doc",
        "sticker": "photo",
        "contact": "doc",
        "location": "doc",
        "share": "doc",
        # легасо-варианты
        "Image": "photo",
        "Video": "video",
        "File": "doc",
        "Sticker": "photo",
        "Doc": "doc",
    }
    return mapping.get((att_type or "").strip(), "other")


def payload_download_info(payload: Any) -> tuple[str, str]:
    """Возвращает (url, file_name) для скачивания вложения MAX."""
    url = getattr(payload, "url", None) or ""
    name = (
        getattr(payload, "file_name", None)
        or getattr(payload, "name", None)
        or getattr(payload, "source", None)
        or ""
    )
    return url, name


async def download_bytes(bot: Any, url: str) -> bytes:
    """Скачивает содержимое по URL (вложения MAX отдаются по прямым ссылкам)."""
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            return await resp.read()


async def download_attachments(
    max_bot: Any, attachments: list[Any] | None
) -> list[DownloadedAttachment]:
    """Скачивает вложения сообщения MAX через API бота."""
    result: list[DownloadedAttachment] = []
    for att in attachments or []:
        payload = getattr(att, "payload", None)
        if payload is None:
            continue
        url, name = payload_download_info(payload)
        if not url:
            continue
        try:
            data = await download_bytes(max_bot, url)
        except Exception:  # noqa: BLE001 - сеть может отказать, пропускаем файл
            continue
        kind = attachment_kind(getattr(att, "type", "") or "")
        if kind == "other":
            kind = "doc"
        result.append(
            DownloadedAttachment(data=data, filename=name or f"attachment.{kind}", kind=kind)
        )
    return result
