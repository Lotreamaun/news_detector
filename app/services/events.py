"""
Журнал событий: виды событий, безопасная запись и суточная очистка.

``record_event`` вызывается одной строкой из любого места и ничего не ждёт: запись идёт
фоновой задачей, сбой пишется в лог предупреждением и не ломает вызывающий код.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from telegram.ext import ContextTypes

from app.models import Event

logger = logging.getLogger(__name__)

# Аудитория
USER_REGISTERED = "user.registered"
USER_VERIFIED = "user.verified"
USER_BLOCKED = "user.blocked"
USER_UNBLOCKED = "user.unblocked"
# Активность
CMD = "cmd"
BUTTON = "button"
SETTINGS_CHANGED = "settings.changed"
WEBAPP_OPEN = "webapp.open"
# Саммари по запросу
SUMMARY_REQUESTED = "summary.requested"
SUMMARY_RESULT = "summary.result"
SUMMARY_LIMIT = "summary.limit"
# Доставка
NOTIFY_SENT = "notify.sent"
NOTIFY_FAILED = "notify.failed"
BROADCAST_STARTED = "broadcast.started"
# Конвейер
CHECK_OK = "check.ok"
CHECK_FAILED = "check.failed"
ACT_PROCESSED = "act.processed"
WEEKLY_SENT = "weekly.sent"

# Сколько дней хранится журнал (Тренду хватает 12 недель, остальное — запас)
EVENT_RETENTION_DAYS = 365

# Ссылки на фоновые задачи записи: без них event loop держит задачу лишь слабой ссылкой
_pending: set[asyncio.Task] = set()


def record_event(
    session_maker,
    kind: str,
    telegram_id: int | None = None,
    ref: str | None = None,
    at: datetime | None = None,
) -> None:
    """Пишет событие в журнал фоновой задачей; ничего не ждёт и не бросает исключений.

    ``at`` по умолчанию — момент вызова (UTC), а не вставки; Telegram-события
    (блокировка) передают время из апдейта. Сбой записи — ``logger.warning``.
    """
    event = Event(
        at=at or datetime.now(timezone.utc),
        kind=kind,
        telegram_id=telegram_id,
        ref=ref[:128] if ref else None,
    )
    try:
        task = asyncio.get_running_loop().create_task(_write_event(session_maker, event))
    except RuntimeError:
        logger.warning("Событие %s не записано: нет запущенного event loop", kind)
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write_event(session_maker, event: Event) -> None:
    """Короткая сессия: INSERT + commit. Исключения не пробрасываются."""
    try:
        async with session_maker() as session:
            session.add(event)
            await session.commit()
    except Exception as exc:
        logger.warning("Событие %s не записано: %s: %s", event.kind, type(exc).__name__, exc)


async def purge_old_events(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк (раз в сутки): удаляет события старше ``EVENT_RETENTION_DAYS``."""
    session_maker = context.bot_data["session_maker"]
    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
    try:
        async with session_maker() as session:
            result = await session.execute(delete(Event).where(Event.at < cutoff))
            await session.commit()
        logger.info("Очистка журнала событий: удалено %d записей старше %d дней", result.rowcount, EVENT_RETENTION_DAYS)
    except Exception:
        logger.warning("Не удалось очистить журнал событий", exc_info=True)
