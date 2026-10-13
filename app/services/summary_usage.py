"""
Счётчик попыток принудительной саммаризации для показа пользователю.

Читает ``summarization_usage`` и ведёт в памяти учёт попыток «в работе» (резерв);
списывает попытки ``force_summary``. Общий источник данных и текстов для бота (``/settings``, отказ по лимиту)
и Mini-App (``POST /usage``, поле ``usage`` в ``POST /summarize``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select

from app.models import SummarizationUsage, User

_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)

NO_SPEND_NOTE = "Попытка не потрачена."

# Платные саммаризации «в работе»: telegram_id -> сколько. Живёт только в памяти процесса.
_reserved: dict[int, int] = {}


def reserve(telegram_id: int) -> None:
    """Занимает попытку пользователя на время платной саммаризации."""
    _reserved[telegram_id] = _reserved.get(telegram_id, 0) + 1


def release(telegram_id: int) -> None:
    """Освобождает занятую попытку; запись удаляется, когда резервов не осталось."""
    left = _reserved.get(telegram_id, 0) - 1
    if left > 0:
        _reserved[telegram_id] = left
    else:
        _reserved.pop(telegram_id, None)


def reserved(telegram_id: int) -> int:
    """Сколько платных саммаризаций пользователя сейчас в работе."""
    return _reserved.get(telegram_id, 0)


@dataclass
class SummaryUsage:
    """Счётчик пользователя: used/limit, признак «без ограничений», дата обновления."""

    used: int
    limit: int
    unlimited: bool
    resets_on: date


def current_month() -> str:
    """Ключ текущего месяца ``YYYY-MM`` (локальное время сервера) — для чтения и списания."""
    return datetime.now().strftime("%Y-%m")


def _next_month_start() -> date:
    """Первое число следующего месяца — по тому же времени, что и ключ месяца."""
    today = datetime.now()
    if today.month == 12:
        return date(today.year + 1, 1, 1)
    return date(today.year, today.month + 1, 1)


async def get_summary_usage(config, session_maker, user: User) -> SummaryUsage:
    """Возвращает счётчик попыток пользователя за текущий месяц.

    Саммаризации «в работе» считаются занятыми попытками (см. ``reserve``).
    """
    limit = config.FORCE_SUMMARIZE_MONTHLY_LIMIT
    unlimited = user.telegram_id in getattr(config, "ADMIN_CHAT_IDS", ())
    async with session_maker() as session:
        count = await session.scalar(
            select(SummarizationUsage.count).where(
                SummarizationUsage.user_id == user.id,
                SummarizationUsage.month == current_month(),
            )
        )
    # гонка двух запросов может дать limit + 1 — «11/10» не показываем
    return SummaryUsage(
        used=min((count or 0) + reserved(user.telegram_id), limit),
        limit=limit,
        unlimited=unlimited,
        resets_on=_next_month_start(),
    )


def format_reset_date(day: date) -> str:
    """«1 ноября»."""
    return f"{day.day} {_MONTHS_GENITIVE[day.month - 1]}"


def limit_message(usage: SummaryUsage) -> str:
    """Текст об исчерпанных попытках — общий для бота и Mini-App."""
    return (
        f"Все попытки саммари на этот месяц использованы ({usage.used}/{usage.limit}). "
        f"Новые появятся {format_reset_date(usage.resets_on)}. "
        "Без попыток доступны готовые саммари, а также ФЗ, ФКЗ и Конституция."
    )
