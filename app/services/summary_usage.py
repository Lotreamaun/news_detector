"""
Счётчик попыток принудительной саммаризации для показа пользователю.

Только читает ``summarization_usage``; списывает попытки ``force_summary``.
Общий источник данных и текстов для бота (``/settings``, отказ по лимиту)
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
    """Возвращает счётчик попыток пользователя за текущий месяц."""
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
        used=min(count or 0, limit),
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
