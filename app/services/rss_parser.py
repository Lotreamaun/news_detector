"""
Работа с pravo.gov.ru: список документов, текст закона, OCR-фоллбэк.

Источники:

- Список документов — JSON API ``publication.pravo.gov.ru/api/Documents``
  (параметры ``PeriodType``/``Block``/страницы и т.д.). Номер опубликования
  ``eoNumber`` используется как внешний ID документа (идемпотентность).
- Текст — редакционный конвейер ``actual.pravo.gov.ru``: сначала
  ``redactions`` (редакции документа), затем ``redtext`` (HTML полного текста).
  Если текст ещё не готов (нет редакции с ``hascontent=true``) или документ
  в реестре отсутствует — возвращается ``None``.
- OCR-фоллбэк — если текст не готов, а документ «важный» (``is_important``),
  PDF (``/file/pdf?eoNumber=...``) загружается в GigaChat как есть, и модель
  извлекает текст со сканов. Пока список «важных» актов пуст (см. TODO).

Модуль не зависит от feedparser/trafilatura — все данные приходят в JSON/HTML.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser

import aiohttp

from app.services.gigachat import GigaChatClient, GigaChatError, classify_reply

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3  # сколько раз повторить запрос при временной ошибке
_REQUEST_TIMEOUT = 30.0  # таймаут HTTP-запроса в секундах
# Ограничиваем текст, чтобы не кормить LLM и не хранить гигантов
_MAX_DOCUMENT_CHARS = 50_000
# Лимит PDF для GigaChat: файл грузится как текстовый документ (40 МБ)
_MAX_PDF_BYTES = 40 * 1024 * 1024
# Максимум токенов на OCR-ответ (полный текст закона со скана)
_MAX_OCR_TOKENS = 4_000

# Базовый URL редакционного API actual.pravo.gov.ru (HTTP_HOST из config.js)
_ACTUAL_BASE_URL = "http://actual.pravo.gov.ru:8000/api/ebpi"
# Страница и PDF документа на publication.pravo.gov.ru
_PUBLICATION_DOCUMENT_URL = "http://publication.pravo.gov.ru/document/{eo}"
_PUBLICATION_PDF_URL = "http://publication.pravo.gov.ru/file/pdf?eoNumber={eo}"

_HEADERS = {"User-Agent": "news_detector/0.1 (+legal-bot)"}

# Заглушка в redtext, когда текст редакции ещё не подготовлен
_REDTEXT_NOT_READY_MARKER = "Подготовка текста редакции"

# GUID типа документа «Федеральный конституционный закон» в справочнике
# publication.pravo.gov.ru (тот же идентификатор, что documentTypeId в API).
_FKZ_DOCUMENT_TYPE_ID = "93273da3-3133-4acf-96c2-4adc1ae70e19"
# GUID для Конституции и ФЗ — уточнятся по мере наблюдения API, пока заглушки
_CONSTITUTION_DOCUMENT_TYPE_ID = "constitution-guid-placeholder"
_FZ_DOCUMENT_TYPE_ID = "82a8bf1c-3bc7-47ed-827f-7affd43a7f27"

# Маппинг documentTypeId → уровень силы (для фильтра law-force-filter)
LEVEL_MAP: dict[str, str] = {
    _FKZ_DOCUMENT_TYPE_ID: "FKZ",
    _CONSTITUTION_DOCUMENT_TYPE_ID: "CONSTITUTION",
    _FZ_DOCUMENT_TYPE_ID: "FZ",
}


def classify_level(document_type_id: str | None) -> str:
    """Определяет уровень силы по documentTypeId, fallback UNKNOWN."""
    if not document_type_id:
        return "UNKNOWN"
    cleaned = str(document_type_id).strip()
    return LEVEL_MAP.get(cleaned, "UNKNOWN")


def classify_level_for_title(title: str | None, document_type_id: str | None = None) -> str:
    """Определяет уровень с fallback по заголовку (для FZ/Конституции, пока GUID не известны)."""
    lvl = classify_level(document_type_id)
    if lvl != "UNKNOWN":
        return lvl
    if not title:
        return "UNKNOWN"
    low = title.lower()
    if "конституц" in low:
        return "CONSTITUTION"
    if "федеральный закон" in low or low.strip().startswith("фз ") or " фз " in f" {low} ":
        return "FZ"
    if "указ президента" in low:
        return "DECREE"
    if "постановление правительства" in low:
        # региональное если есть маркер региона в заголовке
        if any(x in low for x in ["республики", "края", "области", "луганск", "ингушет", "забайкал", "чуваш"]):
            return "REGIONAL"
        return "GOV_RESOLUTION"
    if "приказ" in low:
        if any(x in low for x in ["республики", "края", "области"]):
            return "REGIONAL"
        if "российской федерации" in low or "главного управления" in low or "министерства" in low:
            return "DEPARTMENTAL"
    if any(x in low for x in ["указ главы", "губернатора", "главы "]):
        return "REGIONAL"
    return "UNKNOWN"

# Промпт для OCR-фоллбэка: просим выписать весь текст закона со сканов
_OCR_PROMPT = (
    "Перед тобой скан нормативного правового акта. Распознай и выведи "
    "весь текст документа без изменений и без комментариев: заголовок, "
    "преамбулу, статьи, подпись. Если какой-то фрагмент не читается — "
    "пропускай его и продолжай."
)


class RssError(RuntimeError):
    """Базовая ошибка при работе с pravo.gov.ru."""


class FetchError(RssError):
    """Не удалось выполнить HTTP-запрос (сеть, таймаут, HTTP-ошибка)."""


class ParseError(RssError):
    """Ответ получен, но не является ожидаемым JSON/HTML."""


@dataclass(frozen=True, slots=True)
class FeedEntry:
    """Один документ из API публикаций pravo.gov.ru."""

    external_id: str  # eoNumber — номер опубликования (внешний ID)
    title: str
    url: str  # страница документа на publication.pravo.gov.ru
    published_at: datetime | None  # дата опубликования (aware UTC)
    document_type_id: str | None  # GUID вида акта (для «важности» на Этапе 4)
    signatory_authority_id: str | None  # GUID подписывающего органа
    document_date: datetime | None  # дата самого акта


async def fetch_documents(api_url: str) -> list[FeedEntry]:
    """
    Загружает список документов из JSON API публикаций (одна страница).

    Args:
        api_url: URL API документов (например, ``PRAVO_API_URL`` из конфигурации,
            ``.../api/documents?periodType=daily``).

    Returns:
        Список ``FeedEntry``. Пустой список, если в ответе нет документов.

    Raises:
        FetchError: не удалось загрузить API после всех попыток.
        ParseError: ответ не JSON или не содержит список ``items``.
    """
    raw = await _fetch_with_retries(api_url, what="API документов")
    if not isinstance(raw, str):
        raise ParseError(f"Ожидался текст JSON, получено {type(raw).__name__}: {api_url}")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ParseError(f"Ответ API не является JSON: {api_url}") from exc
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ParseError(f"Ответ API не содержит список items: {api_url}")
    result: list[FeedEntry] = []
    for item in items:
        entry = _entry_from_item(item)
        if entry is not None:
            result.append(entry)
    logger.info("Получено %d документов из API (%s)", len(result), api_url)
    return result


async def fetch_day(day: datetime | str, api_url: str, *, document_type_ids: list[str] | None = None) -> list[FeedEntry]:
    """
    Загружает документы за конкретный календарный день через
    ``periodType=day&date=<dd.mm.yyyy>`` (для бэкфилла 30 дней).

    Args:
        day: день для выборки (date/datetime или строка ``dd.mm.yyyy``).
        api_url: URL API документов (базовый ``.../api/documents``).
        document_type_ids: GUID видов актов для серверного фильтра
            (повторяющийся ``documentTypes=``). Если задан — возвращаются
            только документы этих типов (например ФЗ и ФКЗ), иначе все за день.

    Returns:
        Список ``FeedEntry`` за указанный день.
    """
    if isinstance(day, str):
        day_str = day
    else:
        day_str = day.strftime("%d.%m.%Y")
    # Сбрасываем query из базового URL: periodType=day должен быть единственным
    # (в PRAVO_API_URL уже есть ?PeriodType=daily, иначе он перекроет day).
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(api_url)
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    url = f"{url}?periodType=day&date={day_str}&pageSize=200"
    for tid in (document_type_ids or []):
        url += f"&documentTypes={tid}"
    try:
        entries = await fetch_documents(url)
    except RssError as exc:
        logger.warning("Не удалось получить документы за день %s: %s", day_str, exc)
        return []
    logger.info("Бэкфилл день %s: %d документов", day_str, len(entries))
    return entries


async def get_legal_text(
    external_id: str,
    *,
    actual_base_url: str = _ACTUAL_BASE_URL,
) -> str | None:
    """
    Возвращает полный текст закона с actual.pravo.gov.ru или ``None``.

    Алгоритм: получить редакции документа (``redactions``), выбрать редакцию
    с ``hascontent=true`` и запросить её текст (``redtext``).

    Args:
        external_id: eoNumber документа.
        actual_base_url: базовый URL редакционного API (по умолчанию
            ``http://actual.pravo.gov.ru:8000/api/ebpi``).

    Returns:
        Текст закона (обрезан до ``_MAX_DOCUMENT_CHARS``) или ``None``, если
        текст не готов / документ не найден / произошла сетевая ошибка.
    """
    try:
        redactions = await _fetch_redactions(external_id, actual_base_url)
    except RssError as exc:
        logger.warning("Не удалось получить редакции для %s: %s", external_id, exc)
        return None

    candidates = [r for r in redactions if r.get("hascontent") is True]
    if not candidates:
        logger.info(
            "Текст для %s ещё не готов: нет редакции с hascontent=true", external_id
        )
        return None

    redid = candidates[0].get("redid")
    if not redid:
        logger.warning("Редакция для %s не содержит redid", external_id)
        return None

    try:
        redtext = await _fetch_redtext(redid, actual_base_url)
    except RssError as exc:
        logger.warning("Не удалось получить текст для %s: %s", external_id, exc)
        return None

    if not redtext or _REDTEXT_NOT_READY_MARKER in redtext:
        logger.info("Текст для %s ещё не готов (заглушка редакции)", external_id)
        return None

    full_length = len(redtext)
    text = redtext[:_MAX_DOCUMENT_CHARS]
    logger.info(
        "Получен текст закона %s (%d символов%s)",
        external_id, full_length,
        f", обрезан до {_MAX_DOCUMENT_CHARS}" if full_length > _MAX_DOCUMENT_CHARS else "",
    )
    return text


def is_important(entry: FeedEntry) -> bool:
    """
    «Важные» акты, которым полагается OCR-фоллбэк при отсутствии текста.

    Для MVP — Конституция, ФКЗ и ФЗ (обрабатываются автоматически, без кнопки).
    """
    lvl = classify_level_for_title(entry.title, entry.document_type_id)
    return lvl in ("CONSTITUTION", "FKZ", "FZ")


async def ocr_document_text(
    external_id: str,
    *,
    client: GigaChatClient,
    pdf_url: str | None = None,
) -> str | None:
    """
    OCR-фоллбэк через GigaChat: PDF грузится в хранилище как есть, модель
    извлекает текст со скана. Вызывать только для «важных» актов.

    Args:
        external_id: eoNumber документа.
        client: клиент GigaChat (токен и сессия переиспользуются).
        pdf_url: URL PDF (по умолчанию ``/file/pdf?eoNumber=<id>``).

    Returns:
        Текст закона или ``None`` при любой ошибке (best-effort).
    """
    pdf_bytes = await _download_pdf(external_id, pdf_url=pdf_url)
    if pdf_bytes is None:
        return None

    file_id: str | None = None
    try:
        file_id = await client.upload_file(
            pdf_bytes,
            filename=f"{external_id}.pdf",
            mime="application/pdf",
        )
        reply = await client.complete(
            [{"role": "user", "content": _OCR_PROMPT}],
            attachments=[file_id],
            temperature=0.1,
            max_tokens=_MAX_OCR_TOKENS,
        )
        text = reply.strip()
        _kind = classify_reply(text)
        if _kind == "empty":
            logger.warning("GigaChat вернул пустой OCR-ответ для %s", external_id)
            return None
        if _kind == "refusal":
            logger.warning("GigaChat отказался распознавать текст для %s", external_id)
            return None
        logger.info("OCR GigaChat извлёк текст для %s (%d символов)", external_id, len(text))
        normalized = _normalize_text(text) or text
        return normalized[:_MAX_DOCUMENT_CHARS]
    except GigaChatError as exc:
        logger.warning("OCR через GigaChat не удался для %s: %s", external_id, exc)
        return None
    finally:
        if file_id is not None:
            try:
                await client.delete_file(file_id)
            except GigaChatError:
                logger.warning(
                    "Не удалось удалить файл %s из хранилища GigaChat",
                    file_id,
                    exc_info=True,
                )


# -- Внутреннее -------------------------------------------------------

async def _fetch_redactions(external_id: str, base_url: str) -> list[dict]:
    """Запрашивает список редакций документа; возвращает [] при отсутствии."""
    t = json.dumps({"pnum": external_id, "ttl": 2}, separators=(",", ":"))
    raw = await _fetch_with_retries(
        f"{base_url}/redactions",
        what=f"редакции {external_id}",
        params={"bpa": "ebpi", "t": t},
    )

    if not isinstance(raw, str):
        raise ParseError(f"Ожидался текст JSON, получено {type(raw).__name__}")

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ParseError(f"Ответ redactions не является JSON: {raw[:200]}") from exc

    error = (data.get("error") or "").strip()
    if error:
        if error == "Документ не найден":
            logger.info("Документ %s не найден в actual.pravo.gov.ru", external_id)
        else:
            logger.warning("redactions для %s: %s", external_id, error)
        return []

    redactions = data.get("redactions")
    return redactions if isinstance(redactions, list) else []


async def _fetch_redtext(redid: int | str, base_url: str) -> str | None:
    """Запрашивает HTML текста редакции; важно: t — это просто число (redid)."""
    raw = await _fetch_with_retries(
        f"{base_url}/redtext",
        what=f"текст редакции {redid}",
        params={"bpa": "ebpi", "t": str(redid), "ttl": 2},
    )

    if not isinstance(raw, str):
        raise ParseError(f"Ожидался текст JSON, получено {type(raw).__name__}")

    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ParseError(f"Ответ redtext не является JSON: {raw[:200]}") from exc

    html = data.get("redtext")
    if not isinstance(html, str):
        return None

    return _html_to_text(html)


async def _download_pdf(external_id: str, *, pdf_url: str | None) -> bytes | None:
    """Скачивает PDF документа; None при сетевой ошибке или превышении лимита."""
    url = pdf_url or _PUBLICATION_PDF_URL.format(eo=external_id)
    try:
        data = await _fetch_with_retries(url, what=f"PDF {external_id}", as_bytes=True)
    except FetchError as exc:
        logger.warning("Не удалось скачать PDF %s: %s", external_id, exc)
        return None

    if not isinstance(data, (bytes, bytearray)):
        raise ParseError(f"Ожидались бинарные данные, получено {type(data).__name__}")
    if len(data) > _MAX_PDF_BYTES:
        logger.warning(
            "PDF %s слишком большой (%d байт > %d) — OCR пропускается",
            external_id, len(data), _MAX_PDF_BYTES,
        )
        return None
    return bytes(data)


def _entry_from_item(item: dict) -> FeedEntry | None:
    """Превращает элемент ответа API в FeedEntry; None, если данных мало."""
    external_id = str(item.get("eoNumber") or "").strip()
    if not external_id:
        logger.warning("Пропускаем элемент API без eoNumber: %r", str(item)[:200])
        return None

    title_raw = (item.get("title") or item.get("complexName") or "").strip()
    title = _normalize_text(title_raw) or title_raw
    return FeedEntry(
        external_id=external_id,
        title=title,
        url=_PUBLICATION_DOCUMENT_URL.format(eo=external_id),
        published_at=_parse_api_datetime(item.get("publishDateShort")),
        document_type_id=_clean_id(item.get("documentTypeId")),
        signatory_authority_id=_clean_id(item.get("signatoryAuthorityId")),
        document_date=_parse_api_datetime(item.get("documentDate")),
    )


def _clean_id(value: object) -> str | None:
    """Приводит GUID к строке; None для пустых значений."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_api_datetime(value: object) -> datetime | None:
    """
    Разбирает дату API вида ``2026-08-05T00:00:00`` (наивная, время московское).
    Возвращает aware-datetime в UTC (для упорядочивания). None для пустых/битых.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.startswith("0001-"):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        logger.warning("Не удалось разобрать дату API: %r", text)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class _TextExtractor(HTMLParser):
    """Вытаскивает текст из HTML редакции, вставляя переводы строк на блоках."""

    _BLOCK_TAGS = {
        "p", "div", "br", "tr", "li", "td", "th", "section", "article",
        "h1", "h2", "h3", "h4", "h5", "h6", "table",
    }
    _SKIP_TAGS = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
        elif self._skip_depth == 0 and tag in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = "".join(self._parts)
        raw = re.sub(r"[ \t\u00a0]+", " ", raw)
        lines = [line.strip() for line in raw.splitlines() if line.strip()]
        return "\n".join(lines)


def _html_to_text(html: str) -> str | None:
    """Превращает HTML редакции в читаемый текст; None при неудаче."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        logger.exception("Ошибка разбора HTML редакции (len=%d)", len(html))
        return None
    text = parser.text()
    return _normalize_text(text)


def _normalize_text(text: str | None) -> str | None:
    """
    Приводит сырой текст (OCR-ответ, саммари) к читаемому виду.

    Убирает HTML-теги и сущности, схлопывает множественные пробелы/табы/
    неразрывные пробелы и удаляет избыточные пустые строки. Возвращает None
    только если на входе был пустой/None.
    """
    if not text:
        return None

    # HTML-сущности (в т.ч. &#...;), оставляем обычные амперсанды не-сущностей
    body = re.sub(r"&(#\d+|#x[0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*);", " ", text)
    # Удаляем любые теги, в т.ч. <br>, <br/>, <br /> и остатки разметки
    body = re.sub(r"<[^>]*>", "", body)

    # Схлопываем пробельные последовательности по строкам
    lines = [re.sub(r"[ \t\u00a0]+", " ", line).strip() for line in body.splitlines()]
    # Убираем пустые строки и повторяющиеся пустые строки (оставляем не более одной)
    result_lines: list[str] = []
    for line in lines:
        if line:
            result_lines.append(line)
        elif result_lines and not result_lines[-1]:
            continue
    cleaned = "\n".join(result_lines)
    return cleaned or None


async def _fetch_with_retries(
    url: str,
    *,
    what: str,
    params: dict[str, str] | None = None,
    as_bytes: bool = False,
) -> str | bytes:
    """Загружает URL с повторами на временные ошибки (backoff 1, 2, 4 сек)."""
    last_error: FetchError | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            return await _fetch_once(url, params=params, as_bytes=as_bytes)
        except FetchError as exc:
            last_error = exc
            if attempt == _MAX_RETRIES - 1:
                raise
            logger.warning(
                "Повтор запроса %s (попытка %d/%d): %s",
                what, attempt + 1, _MAX_RETRIES, exc,
            )
            await asyncio.sleep(2**attempt)
    raise last_error  # недостижимо, но типо-безопасно для статики


async def _fetch_once(
    url: str,
    *,
    params: dict[str, str] | None = None,
    as_bytes: bool = False,
) -> str | bytes:
    """Однократная загрузка URL; бросает FetchError при любой проблеме."""
    timeout = aiohttp.ClientTimeout(total=_REQUEST_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params, headers=_HEADERS) as resp:
                if resp.status >= 400:
                    raise FetchError(f"HTTP {resp.status} при запросе {url}")
                if as_bytes:
                    return await resp.read()
                return await resp.text()
    except aiohttp.ClientError as exc:
        raise FetchError(f"Сетевая ошибка при запросе {url}: {exc}") from exc
    except asyncio.TimeoutError as exc:
        raise FetchError(f"Таймаут при запросе {url}") from exc
