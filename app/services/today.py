"""
Правила «законов за сегодня» (чистые функции без HTTP, БД и Telegram).

Здесь: «сегодня» по часовому поясу портала и границы суток под формат хранения
``Article.published_at``, группа акта по номеру публикации (``external_id``),
порядок важности, выбор топ-3 для тизера ``/today`` и группы для страницы
Mini-App. Модуль не импортирует ``scheduler`` и ``handlers``.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Protocol
from zoneinfo import ZoneInfo

from app.services.rss_parser import IMPORTANT_LEVELS

# Часовой пояс портала (дата опубликования — московская), а не настройка рассылки
PORTAL_TZ = ZoneInfo("Europe/Moscow")

FEDERAL = "federal"
REGIONAL = "regional"
OTHER = "other"
GROUP_TITLES: dict[str, str] = {
    FEDERAL: "Федеральные",
    REGIONAL: "Региональные",
    OTHER: "Прочие",
}
_GROUP_ORDER = (FEDERAL, REGIONAL, OTHER)

# eoNumber портала: RR BB YYYYMMDD NNNN (RR — код субъекта, 00 — федеральный уровень)
_EO_NUMBER = re.compile(r"\d{16}")
_FEDERAL_PREFIX = "00"

# Ранг уровня силы внутри федеральных актов: важные → указы → постановления → ведомственные → прочее
_FEDERAL_LEVEL_ORDER = ("DECREE", "GOV_RESOLUTION", "DEPARTMENTAL")


class _ArticleLike(Protocol):
    external_id: str
    level: str
    importance: int | None


def portal_today(now: datetime | None = None) -> date:
    """Сегодняшняя дата по Москве (``now`` — момент времени с часовым поясом, по умолчанию текущий)."""
    return (now or datetime.now(timezone.utc)).astimezone(PORTAL_TZ).date()


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Границы суток ``[D 00:00 UTC, D+1 00:00 UTC)``.

    ``published_at`` хранится как московская дата с полуночью и пометкой UTC, поэтому
    акты даты D лежат именно в этом диапазоне.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def scope_of(external_id: str) -> str:
    """Группа акта по номеру публикации: ``federal`` (код ``00``), ``regional`` или ``other``."""
    if not _EO_NUMBER.fullmatch(external_id or ""):
        return OTHER
    return FEDERAL if external_id.startswith(_FEDERAL_PREFIX) else REGIONAL


def importance_key(article: _ArticleLike) -> tuple:
    """Ключ порядка важности: группа → уровень (только федеральные) → оценка → блок → номер.

    Оценка важности идёт по убыванию, неоценённые (NULL) после оценённых.
    Блок ``BB`` (3–4-я цифры номера: ``00`` высшие органы, ``01`` исполнительные, ...)
    значим для региональных; у остальных групп он не влияет на порядок.
    """
    scope = scope_of(article.external_id)
    if scope == FEDERAL:
        if article.level in IMPORTANT_LEVELS:
            level_rank = 0
        elif article.level in _FEDERAL_LEVEL_ORDER:
            level_rank = 1 + _FEDERAL_LEVEL_ORDER.index(article.level)
        else:
            level_rank = 1 + len(_FEDERAL_LEVEL_ORDER)
    else:
        level_rank = 0
    rating_rank = -article.importance if article.importance is not None else 1
    block = article.external_id[2:4] if scope == REGIONAL else ""
    return (_GROUP_ORDER.index(scope), level_rank, rating_rank, block, article.external_id)


def pick_top(articles: Iterable[_ArticleLike], n: int = 3) -> list:
    """Первые ``n`` самых важных актов дня (для тизера ``/today``)."""
    return sorted(articles, key=importance_key)[:n]


def group_for_page(articles: Iterable[_ArticleLike]) -> list[tuple[str, str, list]]:
    """Группы страницы дня ``(ключ, название, акты)`` в порядке «Федеральные, Региональные, Прочие».

    Федеральные — по ``importance_key``, региональные и прочие — по номеру публикации
    (акты одного субъекта идут подряд). Пустая группа «Прочие» опускается; в пустой
    день групп нет.
    """
    by_scope: dict[str, list] = {scope: [] for scope in _GROUP_ORDER}
    for article in articles:
        by_scope[scope_of(article.external_id)].append(article)
    by_scope[FEDERAL].sort(key=importance_key)
    by_scope[REGIONAL].sort(key=lambda a: a.external_id)
    by_scope[OTHER].sort(key=lambda a: a.external_id)
    if not any(by_scope.values()):
        return []
    return [
        (scope, GROUP_TITLES[scope], by_scope[scope])
        for scope in _GROUP_ORDER
        if by_scope[scope] or scope != OTHER
    ]
