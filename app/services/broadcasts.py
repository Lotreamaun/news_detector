"""
Рассылки администратора: сегменты, проверка черновика, запуск, отправка, ответы и итоги.

Рассылка уходит один раз: при запуске в одной транзакции создаются сама рассылка и строки
доставки (``pending``) по снимку сегмента; цикл отправки переводит каждую строку в
``sent`` / ``blocked`` / ``error`` отдельным commit. После рестарта ``resume_broadcasts``
продолжает отправку по оставшимся ``pending``. Одновременно идёт не больше одной рассылки.
Отправка — тем же ``_send_notification``, что и обычные уведомления (429, 403, журнал).
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from sqlalchemy import exists, func, select, true
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.helpers import escape_markdown

from app.models import Broadcast, BroadcastAnswer, BroadcastDelivery, Event, User
from app.services.events import BROADCAST_STARTED, record_event
from app.services.scheduler import _send_notification
from app.services.stats import ACTIVE_KINDS
from app.services.today import PORTAL_TZ

logger = logging.getLogger(__name__)

# Сегменты аудитории (design.md, решение 2)
SUBSCRIBERS = "subscribers"
ACTIVE_14D = "active_14d"
SILENT_14D = "silent_14d"
UNVERIFIED = "unverified"
SEGMENT_LABELS = {
    SUBSCRIBERS: "Все подписчики",
    ACTIVE_14D: "Активные за 14 дней",
    SILENT_14D: "Молчуны за 14 дней",
    UNVERIFIED: "Не прошли гейт канала",
}
ACTIVE_WINDOW_DAYS = 14

# Ограничения ввода (design.md, решение 7)
TEXT_MAX_LEN = 3500
TELEGRAM_MAX_LEN = 4096
OPTIONS_MAX = 4
BUTTON_MAX_LEN = 40

ACTION_LINK = "link"
ACTION_CHECK_SUBSCRIPTION = "check_subscription"
CHECK_SUBSCRIPTION_LABEL = "Проверить подписку"

SENDING = "sending"
DONE = "done"

PREVIEW_LEN = 120
SEND_PAUSE_SECONDS = 0.05

# Ссылки на задачи отправки (иначе GC может собрать их посреди рассылки) и id идущих рассылок
_tasks: set[asyncio.Task] = set()
_running: set[int] = set()


class BroadcastError(Exception):
    """Отказ с понятной админу причиной; ``code``: ``bad_request`` / ``sending`` / ``empty``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Draft:
    """Проверенный черновик рассылки."""

    text: str
    options: list[str]
    action_kind: str | None
    action_label: str | None
    action_url: str | None
    segment: str


# -- Сегменты -----------------------------------------------------------------

def available_segments(config) -> list[str]:
    """Сегменты, доступные для выбора: ``unverified`` — только при включённом гейте канала."""
    segments = [SUBSCRIBERS, ACTIVE_14D, SILENT_14D]
    if config.REQUIRED_CHANNEL_ID:
        segments.append(UNVERIFIED)
    return segments


async def segment_users(session, config, segment: str) -> list[int]:
    """Telegram ID получателей сегмента (без ``ADMIN_CHAT_IDS``); сегмент должен быть доступен."""
    if segment not in available_segments(config):
        raise BroadcastError("bad_request", "Этот сегмент недоступен.")
    not_admin = User.telegram_id.notin_(config.ADMIN_CHAT_IDS) if config.ADMIN_CHAT_IDS else true()
    stmt = select(User.telegram_id).where(User.is_active.is_(True), not_admin)
    if segment == UNVERIFIED:
        stmt = stmt.where(User.channel_verified.is_(False))
    else:
        # подписчики — как у обычной рассылки уведомлений
        stmt = stmt.where(User.channel_verified.is_(True))
        if segment in (ACTIVE_14D, SILENT_14D):
            since = datetime.now(timezone.utc) - timedelta(days=ACTIVE_WINDOW_DAYS)
            was_active = exists().where(
                Event.telegram_id == User.telegram_id, Event.kind.in_(ACTIVE_KINDS), Event.at >= since
            )
            stmt = stmt.where(was_active if segment == ACTIVE_14D else ~was_active)
    return list((await session.scalars(stmt.order_by(User.telegram_id))).all())


# -- Черновик и кнопки --------------------------------------------------------

def _button_text(value, what: str) -> str:
    text = str(value or "").strip()
    if not 1 <= len(text) <= BUTTON_MAX_LEN:
        raise BroadcastError("bad_request", f"{what}: от 1 до {BUTTON_MAX_LEN} символов.")
    return text


def validate_draft(payload: dict, config) -> Draft:
    """Проверяет черновик из панели; при ошибке бросает ``BroadcastError`` с причиной.

    ``payload``: ``text``, ``options`` (список строк, пустые отбрасываются), ``action``
    (``{kind: link, label, url}`` / ``{kind: check_subscription}`` / null), ``segment``.
    """
    text = str(payload.get("text") or "").strip()
    if not text:
        raise BroadcastError("bad_request", "Текст сообщения пустой.")
    if len(text) > TEXT_MAX_LEN:
        raise BroadcastError("bad_request", f"Текст слишком длинный: {len(text)} из {TEXT_MAX_LEN} символов.")
    if len(escape_markdown(text, version=2)) > TELEGRAM_MAX_LEN:
        raise BroadcastError("bad_request", "Текст не помещается в сообщение Telegram: сократите его.")

    raw_options = payload.get("options") or []
    if not isinstance(raw_options, list):
        raise BroadcastError("bad_request", "Некорректные варианты ответа.")
    options = [str(o).strip() for o in raw_options if str(o or "").strip()]
    if len(options) > OPTIONS_MAX:
        raise BroadcastError("bad_request", f"Вариантов ответа не больше {OPTIONS_MAX}.")
    options = [_button_text(o, "Вариант ответа") for o in options]
    if len({o.casefold() for o in options}) != len(options):
        raise BroadcastError("bad_request", "Варианты ответа повторяются.")

    action = payload.get("action") or {}
    if not isinstance(action, dict):
        raise BroadcastError("bad_request", "Некорректная кнопка действия.")
    kind = action.get("kind") or None
    label = url = None
    if kind == ACTION_LINK:
        label = _button_text(action.get("label"), "Подпись кнопки-ссылки")
        url = str(action.get("url") or "").strip()
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.netloc:
            raise BroadcastError("bad_request", "Адрес ссылки должен начинаться с https://")
    elif kind == ACTION_CHECK_SUBSCRIPTION:
        if not config.REQUIRED_CHANNEL_ID:
            raise BroadcastError("bad_request", "Гейт канала выключен: кнопка «Проверить подписку» не нужна.")
        label = CHECK_SUBSCRIPTION_LABEL
    elif kind is not None:
        raise BroadcastError("bad_request", "Неизвестная кнопка действия.")

    segment = str(payload.get("segment") or "")
    if segment not in available_segments(config):
        raise BroadcastError("bad_request", "Выберите аудиторию из списка.")
    return Draft(text, options, kind, label, url, segment)


def build_markup(
    broadcast_id: int,
    options: list[str],
    action_kind: str | None,
    action_label: str | None,
    action_url: str | None,
    selected: int | None = None,
) -> InlineKeyboardMarkup | None:
    """Кнопки рассылки: варианты ``bc:<id>:<n>`` по одному в строке (выбранный — с «✓ »), действие последним.

    Для тестовой отправки ``broadcast_id = 0``. Без кнопок — ``None``.
    """
    rows = [
        [InlineKeyboardButton(f"✓ {option}" if n == selected else option, callback_data=f"bc:{broadcast_id}:{n}")]
        for n, option in enumerate(options)
    ]
    if action_kind == ACTION_LINK:
        rows.append([InlineKeyboardButton(action_label, url=action_url)])
    elif action_kind == ACTION_CHECK_SUBSCRIPTION:
        rows.append([InlineKeyboardButton(action_label, callback_data="check_subscription")])
    return InlineKeyboardMarkup(rows) if rows else None


def _broadcast_markup(broadcast: Broadcast, selected: int | None = None) -> InlineKeyboardMarkup | None:
    return build_markup(
        broadcast.id, json.loads(broadcast.options), broadcast.action_kind,
        broadcast.action_label, broadcast.action_url, selected,
    )


# -- Тест и запуск ------------------------------------------------------------

async def send_test(bot, admin_id: int, draft: Draft) -> None:
    """Отправляет черновик только администратору (варианты ``bc:0:<n>``), в БД ничего не пишет.

    Не через ``_send_notification``: тест не должен деактивировать админа и попадать в журнал.
    """
    await bot.send_message(
        chat_id=admin_id,
        text=escape_markdown(draft.text, version=2),
        reply_markup=build_markup(0, draft.options, draft.action_kind, draft.action_label, draft.action_url),
        parse_mode="MarkdownV2",
    )


async def start_broadcast(
    session_maker, config, admin_id: int, draft: Draft, client_token: str
) -> tuple[Broadcast, bool]:
    """Создаёт рассылку и строки доставки по снимку сегмента одной транзакцией.

    Возвращает ``(рассылка, создана ли сейчас)``: повтор ``client_token`` отдаёт уже созданную.
    Бросает ``BroadcastError``: ``sending`` — идёт другая рассылка, ``empty`` — в сегменте никого.
    """
    async with session_maker() as session:
        existing = await session.scalar(select(Broadcast).where(Broadcast.client_token == client_token))
        if existing is not None:
            return existing, False
        if await session.scalar(select(Broadcast.id).where(Broadcast.status == SENDING).limit(1)):
            raise BroadcastError("sending", "Идёт отправка другой рассылки — дождитесь её окончания.")
        recipients = await segment_users(session, config, draft.segment)
        if not recipients:
            raise BroadcastError("empty", "В этом сегменте нет получателей.")
        broadcast = Broadcast(
            created_at=datetime.now(timezone.utc),
            created_by=admin_id,
            text=draft.text,
            options=json.dumps(draft.options, ensure_ascii=False),
            action_kind=draft.action_kind,
            action_label=draft.action_label,
            action_url=draft.action_url,
            segment=draft.segment,
            recipients=len(recipients),
            status=SENDING,
            client_token=client_token,
        )
        session.add(broadcast)
        try:
            await session.flush()
            session.add_all(
                BroadcastDelivery(broadcast_id=broadcast.id, telegram_id=tid, status="pending") for tid in recipients
            )
            await session.commit()
        except IntegrityError:
            # тот же client_token вставили параллельным запросом
            await session.rollback()
            existing = await session.scalar(select(Broadcast).where(Broadcast.client_token == client_token))
            if existing is None:
                raise
            return existing, False
    record_event(session_maker, BROADCAST_STARTED, ref=f"{broadcast.id}:{broadcast.recipients}")
    logger.info(
        "Рассылка #%d запущена админом %s: сегмент %s, получателей %d",
        broadcast.id, admin_id, broadcast.segment, broadcast.recipients,
    )
    return broadcast, True


# -- Отправка -----------------------------------------------------------------

async def run_broadcast(bot, session_maker, broadcast_id: int) -> None:
    """Отправляет рассылку всем ``pending``-получателям и закрывает её (``done``).

    Статус строки пишется сразу после отправки: ``True`` → ``sent``, ``None`` (403) → ``blocked``,
    ``False`` → ``error``. Исключение на одном получателе — ``error``, отправка идёт дальше.
    """
    async with session_maker() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None or broadcast.status != SENDING:
            return
        pending = list(
            (
                await session.scalars(
                    select(BroadcastDelivery.telegram_id)
                    .where(BroadcastDelivery.broadcast_id == broadcast_id, BroadcastDelivery.status == "pending")
                    .order_by(BroadcastDelivery.telegram_id)
                )
            ).all()
        )
    text = escape_markdown(broadcast.text, version=2)
    markup = _broadcast_markup(broadcast)
    logger.info("Рассылка #%d: отправка %d получателям", broadcast_id, len(pending))

    for telegram_id in pending:
        try:
            # не персистентный User: доставке нужен только telegram_id
            result = await _send_notification(bot, session_maker, User(telegram_id=telegram_id), text, markup, "broadcast")
            status = "sent" if result else ("blocked" if result is None else "error")
        except Exception:
            logger.exception("Рассылка #%d: ошибка отправки %s — пропуск", broadcast_id, telegram_id)
            status = "error"
        async with session_maker() as session:
            delivery = await session.get(BroadcastDelivery, (broadcast_id, telegram_id))
            delivery.status = status
            delivery.sent_at = datetime.now(timezone.utc)
            await session.commit()
        await asyncio.sleep(SEND_PAUSE_SECONDS)

    async with session_maker() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        broadcast.status = DONE
        broadcast.finished_at = datetime.now(timezone.utc)
        await session.commit()
    logger.info("Рассылка #%d завершена", broadcast_id)


async def _run_guarded(bot, session_maker, broadcast_id: int) -> None:
    try:
        await run_broadcast(bot, session_maker, broadcast_id)
    except Exception:
        # статус остаётся sending — отправка продолжится после рестарта
        logger.exception("Рассылка #%d прервана", broadcast_id)
    finally:
        _running.discard(broadcast_id)


def spawn_broadcast(bot, session_maker, broadcast_id: int) -> None:
    """Запускает отправку фоновой задачей; рассылку, которая уже отправляется, второй раз не запускает."""
    if broadcast_id in _running:
        return
    _running.add(broadcast_id)
    task = asyncio.get_running_loop().create_task(_run_guarded(bot, session_maker, broadcast_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume_broadcasts(bot, session_maker) -> None:
    """После рестарта продолжает все рассылки в ``sending`` (только по ``pending``-строкам)."""
    async with session_maker() as session:
        ids = list((await session.scalars(select(Broadcast.id).where(Broadcast.status == SENDING))).all())
    for broadcast_id in ids:
        logger.info("Рассылка #%d не завершена до рестарта — продолжаем", broadcast_id)
        spawn_broadcast(bot, session_maker, broadcast_id)


# -- Ответы и итоги -----------------------------------------------------------

async def record_answer(session_maker, broadcast_id: int, telegram_id: int, option: int) -> InlineKeyboardMarkup | None:
    """Сохраняет ответ получателя (один на человека, последний заменяет прежний).

    Возвращает кнопки с отмеченным вариантом для перерисовки; ``None`` — ответ не принят
    (нет рассылки, варианта или пользователь не получатель этой рассылки).
    """
    async with session_maker() as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast is None or not 0 <= option < len(json.loads(broadcast.options)):
            return None
        if await session.get(BroadcastDelivery, (broadcast_id, telegram_id)) is None:
            return None
        now = datetime.now(timezone.utc)
        answer = await session.get(BroadcastAnswer, (broadcast_id, telegram_id))
        if answer is None:
            session.add(BroadcastAnswer(broadcast_id=broadcast_id, telegram_id=telegram_id, option=option, answered_at=now))
        else:
            answer.option = option
            answer.answered_at = now
        await session.commit()
    return _broadcast_markup(broadcast, selected=option)


def _as_utc(value: datetime) -> datetime:
    """SQLite отдаёт даты без tzinfo (хранятся в UTC) — приводим к aware UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def list_broadcasts(session_maker, limit: int = 20) -> list[dict]:
    """Последние рассылки (новые сверху) с итогами доставки и ответами по вариантам."""
    async with session_maker() as session:
        broadcasts = list(
            (await session.scalars(select(Broadcast).order_by(Broadcast.id.desc()).limit(limit))).all()
        )
        ids = [b.id for b in broadcasts]
        deliveries = await session.execute(
            select(BroadcastDelivery.broadcast_id, BroadcastDelivery.status, func.count())
            .where(BroadcastDelivery.broadcast_id.in_(ids))
            .group_by(BroadcastDelivery.broadcast_id, BroadcastDelivery.status)
        )
        answers = await session.execute(
            select(BroadcastAnswer.broadcast_id, BroadcastAnswer.option, func.count())
            .where(BroadcastAnswer.broadcast_id.in_(ids))
            .group_by(BroadcastAnswer.broadcast_id, BroadcastAnswer.option)
        )
    counts = {b_id: {"sent": 0, "blocked": 0, "error": 0, "pending": 0} for b_id in ids}
    for b_id, status, count in deliveries.all():
        counts[b_id][status] = count
    answered: dict[tuple[int, int], int] = {(b_id, option): count for b_id, option, count in answers.all()}

    result = []
    for b in broadcasts:
        sent = counts[b.id]["sent"]
        options = []
        for n, option in enumerate(json.loads(b.options)):
            count = answered.get((b.id, n), 0)
            options.append({"text": option, "count": count, "share": round(count * 100 / sent) if sent else 0})
        created = _as_utc(b.created_at)
        result.append({
            "id": b.id,
            "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time": created.astimezone(PORTAL_TZ).strftime("%d.%m %H:%M"),
            "segment": b.segment,
            "segment_label": SEGMENT_LABELS.get(b.segment, b.segment),
            "preview": b.text if len(b.text) <= PREVIEW_LEN else b.text[:PREVIEW_LEN].rstrip() + "…",
            "action_label": b.action_label,
            "recipients": b.recipients,
            "status": b.status,
            "counts": counts[b.id],
            "options": options,
        })
    return result
