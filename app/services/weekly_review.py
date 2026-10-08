"""
Правила модерации недельной подборки администратором (чистые функции без HTTP и без БД).

Решение администратора хранится в ``Article.digest_override`` (``'include'`` /
``'exclude'`` / ``None``) и окончательно: модель его не пересматривает. Здесь —
итоговый выбор подборки (его используют рассылка, панель, экспорт и предпросмотр,
чтобы все показывали одно и то же), целевой период панели, краткое название
документа и Markdown для поста в канал. Модуль не импортирует ``scheduler``.
"""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import NamedTuple
from zoneinfo import ZoneInfo

from app.models import Article
from app.services.rss_parser import _normalize_text
from app.services.weekly_window import (
    DAY_END,
    last_digest_moment,
    next_digest_moment,
    previous_digest_moment,
    sunday_at,
)

# Недельная подборка автоматически включает акты с оценкой не ниже этой
WEEKLY_MIN_IMPORTANCE = 2
INCLUDE = "include"
EXCLUDE = "exclude"
# Предел вручную введённого саммари: тизер из трёх законов должен уложиться в лимит
# сообщения Telegram (4096 символов), см. design.md, п. 12
SUMMARY_MANUAL_MAX_LEN = 600


# callback_data кнопки «🔗 Ссылка для браузера» в напоминании (обработчик — app/bot/handlers.py)
ADMIN_LINK_CALLBACK = "admin:link"


def moderation_unavailable_reason(config) -> str | None:
    """Почему модерация недоступна (для лога при старте); ``None`` — доступна.

    Нужны время рассылки (иначе подборки нет), ``WEBAPP_URL`` (адрес панели) и
    хотя бы один администратор.
    """
    if config.WEEKLY_DIGEST_TIME is None:
        return "WEEKLY_DIGEST_TIME не задан"
    if not config.WEBAPP_URL:
        return "WEBAPP_URL не задан"
    if not config.ADMIN_CHAT_IDS:
        return "ADMIN_CHAT_IDS пуст"
    return None


def is_awaiting_rating(article: Article) -> bool:
    """Кандидат ждёт оценки модели: нет ни оценки, ни решения администратора."""
    return article.importance is None and article.digest_override is None


def needs_summary_retry(article: Article) -> bool:
    """Важный акт без саммари, который пойдёт в подборку (не исключён) — ему нужна «попытка 2»."""
    return (
        article.importance is not None
        and article.importance >= WEEKLY_MIN_IMPORTANCE
        and article.digest_override != EXCLUDE
        and not article.summary
    )


def _rank(article: Article) -> int:
    """Ранг порядка подборки: оценка, без оценки — ``-1``."""
    return -1 if article.importance is None else article.importance


def _order(articles: list[Article]) -> list[Article]:
    """Сначала выше оценка, внутри оценки — более поздняя дата публикации."""
    return sorted(
        articles,
        key=lambda a: (_rank(a), a.published_at.timestamp() if a.published_at else 0.0),
        reverse=True,
    )


def final_selection(window: list[Article]) -> list[Article]:
    """Итоговый набор подборки: документы окна с саммари, включённые администратором
    либо (без решения) с оценкой не ниже ``WEEKLY_MIN_IMPORTANCE``; в порядке подборки."""
    chosen = [
        a
        for a in window
        if a.summary
        and (
            a.digest_override == INCLUDE
            or (
                a.digest_override is None
                and a.importance is not None
                and a.importance >= WEEKLY_MIN_IMPORTANCE
            )
        )
    ]
    return _order(chosen)


def split_sections(window: list[Article]) -> tuple[list[Article], list[Article], list[Article]]:
    """Разделы панели: «В подборке», «Ещё не оценены», «Не вошли» (всё остальное окно)."""
    chosen = final_selection(window)
    chosen_ids = {a.id for a in chosen}
    unrated = _order([a for a in window if a.id not in chosen_ids and is_awaiting_rating(a)])
    unrated_ids = {a.id for a in unrated}
    rest = _order([a for a in window if a.id not in chosen_ids and a.id not in unrated_ids])
    return chosen, unrated, rest


class ReviewCounts(NamedTuple):
    included: int  # законов в подборке
    no_summary: int  # важных (пойдут в подборку) без саммари
    unrated: int  # кандидатов без оценки и без решения


def review_counts(window: list[Article]) -> ReviewCounts:
    """Счётчики сводки панели и напоминания."""
    return ReviewCounts(
        included=len(final_selection(window)),
        no_summary=sum(1 for a in window if needs_summary_retry(a)),
        unrated=sum(1 for a in window if is_awaiting_rating(a)),
    )


class TargetPeriod(NamedTuple):
    period_end: datetime  # запланированный момент отправки
    window_start: datetime
    window_end: datetime  # конец окна: период_end для отправляемого, «сейчас» для накапливаемого
    sending: bool  # отправка этого периода уже началась — правки отклоняются


def target_period(
    now: datetime,
    send_time: time,
    tz: ZoneInfo,
    run_started: datetime | None,
    run_finished: datetime | None,
) -> TargetPeriod:
    """Какую подборку показывает панель (design.md, п. 5).

    ``run_started`` / ``run_finished`` — отметки ``weekly_digest_runs`` для последнего
    наступившего запланированного момента (``None``, если записи нет). Пока отправка за
    него не завершена и не прошёл её жёсткий срок (воскресенье ``DAY_END``), целевой
    период — он (отправка отложена или идёт); иначе — следующий, который накапливается.
    """
    last = last_digest_moment(now, send_time, tz)
    if run_finished is None and now <= sunday_at(last, DAY_END):
        return TargetPeriod(last, previous_digest_moment(last), last, run_started is not None)
    return TargetPeriod(next_digest_moment(last), last, now, False)


def format_time_left(delta: timedelta) -> str:
    """«3 ч 12 мин», «45 мин», «менее минуты»."""
    minutes = int(delta.total_seconds() // 60)
    if minutes <= 0:
        return "менее минуты"
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append(f"{days} д")
    if hours:
        parts.append(f"{hours} ч")
    if mins and not days:
        parts.append(f"{mins} мин")
    return " ".join(parts)


# «Федеральный закон от 04.08.2026 № 282-ФЗ "О цифровых…"» -> вид, номер, предмет
_TITLE_RE = re.compile(
    r'^(?P<kind>.+?)\s+от\s+.+?\s+№\s*(?P<num>\S+)\s+(?P<subject>["«].+)$', re.S
)


def _strip_outer_quotes(subject: str) -> str:
    """Снимает внешние кавычки предмета, не ломая вложенные (парность проверяется по счёту)."""
    subject = subject[1:]  # открывающая кавычка гарантирована шаблоном
    if subject.endswith('"') and subject.count('"') % 2 == 1:
        subject = subject[:-1]
    elif subject.endswith("»") and subject.count("»") > subject.count("«"):
        subject = subject[:-1]
    return subject.strip()


def short_title(title: str) -> str:
    """Краткое название: «Вид акта № номер · Предмет»; если шаблон не узнан — полное название."""
    clean = _normalize_text(title) or title
    m = _TITLE_RE.match(clean)
    if not m:
        return clean
    subject = _strip_outer_quotes(m.group("subject").strip())
    if not subject:
        return clean
    kind = m.group("kind").replace("Российской Федерации", "РФ")
    return f"{kind} № {m.group('num')} · {subject}"


def build_export_markdown(articles: list[Article]) -> str:
    """Markdown итогового набора для поста: «**Название** / саммари / [Читать на портале](url)»,
    блоки через пустую строку, порядок как в подборке (название — как в рассылке)."""
    blocks = []
    for a in articles:
        title = _normalize_text(a.title) or a.title
        summary = _normalize_text(a.summary) or a.summary or ""
        blocks.append(f"**{title}**\n{summary}\n[Читать на портале]({a.url})")
    return "\n\n".join(blocks)
