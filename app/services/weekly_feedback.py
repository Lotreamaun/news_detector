"""
Оценка недельной подборки кнопками: клавиатура, запись голоса и итоги по периодам.

Период — строка ``weekly_digest_runs`` (``run_id`` в ``callback_data``). Клавиатура у каждого
пользователя своя: строки оценки перестраиваются по сохранённому голосу, остальные кнопки
сообщения («📖 Дайджест») переносятся как есть. ``run_id = 0`` — предпросмотр администратора.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from sqlalchemy import distinct, func, select, true
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.models import Event, WeeklyDigestRun, WeeklyFeedback
from app.services.events import NOTIFY_SENT
from app.services.today import PORTAL_TZ

PREFIX = "wf"
UP = "up"
DOWN = "down"
# Причины 👎: индекс в этом списке хранится в ``weekly_feedback.reason``
REASONS = ("Много лишнего", "Не хватает важного", "Сложно читать", "Не моя сфера")
LABEL_UP = "👍 Полезно"
LABEL_DOWN = "👎 Не то"
TOAST_UP = "Спасибо!"
TOAST_DOWN = "Спасибо! Можно выбрать причину ниже"
TOAST_REASON = "Спасибо, учтём"
TOAST_PREVIEW = "Предпросмотр — оценка не сохраняется"
SUMMARY_LIMIT = 8

_CALLBACK = re.compile(rf"^{PREFIX}:(\d+):(up|down|r([0-3]))$")


def _mark(label: str, chosen: bool) -> str:
    return f"✓ {label}" if chosen else label


def feedback_rows(run_id: int, verdict: str | None, reason: int | None) -> list[list[InlineKeyboardButton]]:
    """Строки оценки: 👍/👎 с ✓ у выбранного; при 👎 — две строки причин по 2 с ✓ у выбранной."""
    rows = [
        [
            InlineKeyboardButton(_mark(LABEL_UP, verdict == UP), callback_data=f"{PREFIX}:{run_id}:{UP}"),
            InlineKeyboardButton(_mark(LABEL_DOWN, verdict == DOWN), callback_data=f"{PREFIX}:{run_id}:{DOWN}"),
        ]
    ]
    if verdict == DOWN:
        buttons = [
            InlineKeyboardButton(_mark(label, reason == i), callback_data=f"{PREFIX}:{run_id}:r{i}")
            for i, label in enumerate(REASONS)
        ]
        rows += [buttons[:2], buttons[2:]]
    return rows


def merge_markup(markup: InlineKeyboardMarkup | None, rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    """Строки исходной клавиатуры без кнопок ``wf:`` плюс новые строки оценки."""
    kept = [
        list(row)
        for row in (markup.inline_keyboard if markup else ())
        if not any((b.callback_data or "").startswith(f"{PREFIX}:") for b in row)
    ]
    return InlineKeyboardMarkup(kept + rows)


def parse_callback(data: str | None) -> tuple[int, str] | None:
    """``wf:<run_id>:up|down|r<0-3>`` → ``(run_id, action)``; всё прочее — ``None``."""
    match = _CALLBACK.match(data or "")
    return (int(match.group(1)), match.group(2)) if match else None


async def record_vote(session_maker, run_id: int, telegram_id: int, action: str) -> tuple[str, int | None] | None:
    """Сохраняет голос (один на человека за период, последний заменяет прежний).

    ``up`` сбрасывает причину, ``down`` сохраняет прежнюю, ``r<n>`` ставит ``down`` и причину.
    Возвращает ``(verdict, reason)`` для перерисовки; ``None`` — периода нет, голос не принят.
    """
    async with session_maker() as session:
        if await session.get(WeeklyDigestRun, run_id) is None:
            return None
        vote = await session.get(WeeklyFeedback, (run_id, telegram_id))
        if vote is None:
            vote = WeeklyFeedback(run_id=run_id, telegram_id=telegram_id, verdict=UP, reason=None)
            session.add(vote)
        if action == UP:
            vote.verdict, vote.reason = UP, None
        elif action == DOWN:
            vote.verdict = DOWN
        else:
            vote.verdict, vote.reason = DOWN, int(action[1:])
        vote.answered_at = datetime.now(timezone.utc)
        result = (vote.verdict, vote.reason)
        await session.commit()
    return result


def _utc(value: datetime) -> datetime:
    """SQLite отдаёт даты без tzinfo (хранятся в UTC) — приводим к aware UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def feedback_summary(session, config, limit: int = SUMMARY_LIMIT) -> list[dict]:
    """Итоги оценок по последним отправленным подборкам (новые сверху) для «Тренда».

    «Получили» — разные пользователи с ``notify.sent`` (``ref = weekly``) между началом и концом
    рассылки периода. Администраторы исключены из получивших и из оценок; периоды без получивших
    пропускаются.
    """
    def not_admin(column):
        return column.notin_(config.ADMIN_CHAT_IDS) if config.ADMIN_CHAT_IDS else true()

    runs = (
        await session.scalars(
            select(WeeklyDigestRun)
            .where(WeeklyDigestRun.finished_at.isnot(None))
            .order_by(WeeklyDigestRun.period_end.desc())
            .limit(limit)
        )
    ).all()
    result = []
    for run in runs:
        received = await session.scalar(
            select(func.count(distinct(Event.telegram_id))).where(
                Event.kind == NOTIFY_SENT,
                Event.ref == "weekly",
                Event.at >= _utc(run.started_at),
                Event.at <= _utc(run.finished_at),
                not_admin(Event.telegram_id),
            )
        )
        if not received:
            continue
        rows = await session.execute(
            select(WeeklyFeedback.verdict, WeeklyFeedback.reason, func.count())
            .where(WeeklyFeedback.run_id == run.id, not_admin(WeeklyFeedback.telegram_id))
            .group_by(WeeklyFeedback.verdict, WeeklyFeedback.reason)
        )
        up = down = no_reason = 0
        by_reason = [0] * len(REASONS)
        for verdict, reason, count in rows.all():
            if verdict == UP:
                up += count
                continue
            down += count
            if reason is None:
                no_reason += count
            elif 0 <= reason < len(REASONS):
                by_reason[reason] += count
        result.append(
            {
                "date": _utc(run.period_end).astimezone(PORTAL_TZ).date().isoformat(),
                "received": received,
                "up": up,
                "down": down,
                "reasons": [{"label": label, "count": n} for label, n in zip(REASONS, by_reason)],
                "no_reason": no_reason,
            }
        )
    return result
