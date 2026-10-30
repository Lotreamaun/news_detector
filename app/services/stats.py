"""
Статистика для админ-панели: «Пульс» (сейчас) и «Тренд» (по неделям) по журналу событий.

Всё считается агрегирующими SQL-запросами по запросу, без предрасчёта. Границы «сегодня»,
«7 дней» и недель считаются по Москве и передаются в SQL как UTC-моменты. Администраторы
(``ADMIN_CHAT_IDS`` на момент запроса) из метрик с пользователями исключены; метрики
конвейера (``check.*``, ``act.processed``, ``weekly.sent``) не фильтруются — у них нет пользователя.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from sqlalchemy import Integer, and_, case, cast, distinct, exists, false, func, select, true

from app.models import Article, Event, User, UserFilter, WeeklyDigestRun
from app.services import events as ev
from app.services.today import PORTAL_TZ, portal_today

# Пояснения к метрикам — `HINTS` в webapp/static/admin.html: меняешь подсчёт — поправь текст
# Виды событий, по которым пользователь считается активным
ACTIVE_KINDS = (ev.CMD, ev.BUTTON, ev.SETTINGS_CHANGED, ev.WEBAPP_OPEN, ev.SUMMARY_REQUESTED)
# Массовые события, которые не попадают в ленту (их отражают плитки и строка проверки)
FEED_EXCLUDED_KINDS = (ev.CHECK_OK, ev.NOTIFY_SENT, ev.ACT_PROCESSED)
FEED_SIZE = 50
TREND_WEEKS = 12
# Проверка pravo.gov.ru «требует внимания», если последнее событие старше стольких интервалов
CHECK_STALE_FACTOR = 3

_WEBAPP_VIEWS = {"law": "закон", "digest": "дайджест", "today": "все за сегодня"}
_SUMMARY_STATUSES = {"ok": "готово", "no_text": "нет текста", "refused": "отказ модели", "error": "ошибка"}
_NOTIFY_FAILURES = {"blocked": "заблокировал бота", "error": "ошибка отправки"}


# -- Время ------------------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    """SQLite отдаёт даты без tzinfo (хранятся в UTC) — приводим к aware UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _msk_midnight(day: date) -> datetime:
    """Полночь московского дня ``day`` как момент UTC."""
    return datetime.combine(day, time.min, tzinfo=PORTAL_TZ).astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    """UTC-момент в ISO-строке с ``Z`` для фронта."""
    return _as_utc(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bucket(column, origin: datetime):
    """Номер недели от ``origin`` (UTC) для столбца-даты; считать только строки ``>= origin``."""
    origin_text = origin.strftime("%Y-%m-%d %H:%M:%S")
    return cast((func.julianday(column) - func.julianday(origin_text)) / 7, Integer)


# -- Общие условия ----------------------------------------------------------

def _not_admin(column, config):
    """Условие «не администратор» по столбцу telegram_id; без администраторов — без условия."""
    return column.notin_(config.ADMIN_CHAT_IDS) if config.ADMIN_CHAT_IDS else true()


def _is_blocked():
    """SQL-выражение «пользователь заблокировал бота».

    Последнее по времени событие ``user.blocked`` / ``user.unblocked`` этого ``telegram_id``;
    если таких событий нет — рассылка отключила ему уведомления (``is_active = False``).
    """
    last_kind = (
        select(Event.kind)
        .where(Event.telegram_id == User.telegram_id, Event.kind.in_((ev.USER_BLOCKED, ev.USER_UNBLOCKED)))
        .order_by(Event.at.desc(), Event.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    return case(
        (last_kind == ev.USER_BLOCKED, true()),
        (last_kind == ev.USER_UNBLOCKED, false()),
        else_=User.is_active.is_(False),
    )


def _is_subscriber():
    """Подписчик — тот, кому реально уходят уведомления."""
    return and_(User.channel_verified.is_(True), User.is_active.is_(True), ~_is_blocked())


async def _count_events(
    session,
    config,
    kinds: tuple[str, ...],
    start: datetime,
    *,
    ref: str | None = None,
    unique_users: bool = False,
) -> int:
    """Число событий видов ``kinds`` с момента ``start`` без администраторов; ``unique_users`` — разных пользователей."""
    column = func.count(distinct(Event.telegram_id)) if unique_users else func.count()
    stmt = select(column).where(Event.kind.in_(kinds), Event.at >= start)
    if ref is not None:
        stmt = stmt.where(Event.ref == ref)
    stmt = stmt.where(_not_admin(Event.telegram_id, config))
    return await session.scalar(stmt) or 0


# -- Пульс ------------------------------------------------------------------

async def pulse(session_maker, config, now: datetime | None = None) -> dict:
    """Данные вкладки «Пульс»: аудитория, вовлечённость, доставка, конвейер, фильтры, лента."""
    now = now or datetime.now(timezone.utc)
    today = portal_today(now)
    periods = {
        "today": _msk_midnight(today),
        "week": _msk_midnight(today - timedelta(days=6)),
    }
    month_start = _msk_midnight(today.replace(day=1))
    users_ok = _not_admin(User.telegram_id, config)

    async with session_maker() as session:
        async def per_period(count) -> dict[str, int]:
            return {name: await count(start) for name, start in periods.items()}

        since = await session.scalar(select(func.min(Event.at)))

        # аудитория
        registered = await session.scalar(select(func.count()).select_from(User).where(users_ok)) or 0
        unverified = await session.scalar(
            select(func.count()).select_from(User).where(users_ok, User.channel_verified.is_(False))
        ) or 0
        blocked = await session.scalar(select(func.count()).select_from(User).where(users_ok, _is_blocked())) or 0
        subscribers = await session.scalar(
            select(func.count()).select_from(User).where(users_ok, _is_subscriber())
        ) or 0
        new_users = await per_period(
            lambda start: session.scalar(
                select(func.count()).select_from(User).where(users_ok, User.created_at >= start)
            )
        )
        blocked_new = await per_period(
            lambda start: _count_events(session, config, (ev.USER_BLOCKED,), start, unique_users=True)
        )

        # вовлечённость
        active = await per_period(
            lambda start: _count_events(session, config, ACTIVE_KINDS, start, unique_users=True)
        )
        opens = await per_period(lambda start: _count_events(session, config, (ev.WEBAPP_OPEN,), start))
        summaries = await per_period(
            lambda start: _count_events(session, config, (ev.SUMMARY_RESULT,), start, ref="ok")
        )
        limit_month = await _count_events(session, config, (ev.SUMMARY_LIMIT,), month_start, unique_users=True)

        # доставка
        sent = await per_period(lambda start: _count_events(session, config, (ev.NOTIFY_SENT,), start))
        failed = await per_period(lambda start: _count_events(session, config, (ev.NOTIFY_FAILED,), start))

        pipeline = await _pipeline(session, config, now)
        filters = await _filters(session, users_ok)
        feed = await _feed(session, config)

    return {
        "generated_at": _iso(now),
        "tracking_since": _iso(since) if since else None,
        "audience": {
            "subscribers": subscribers,
            "registered": registered,
            "unverified": unverified,
            "blocked": blocked,
            "new": new_users,
            "blocked_new": blocked_new,
        },
        "engagement": {"active": active, "opens": opens, "summaries": summaries, "limit_month": limit_month},
        "delivery": {"sent": sent, "failed": failed},
        "pipeline": pipeline,
        "filters": filters,
        "feed": feed,
    }


async def _pipeline(session, config, now: datetime) -> dict:
    """Последняя проверка pravo.gov.ru и акты за 24 часа по источнику текста и исходу саммари."""
    last = (
        await session.execute(
            select(Event.at, Event.kind, Event.ref)
            .where(Event.kind.in_((ev.CHECK_OK, ev.CHECK_FAILED)))
            .order_by(Event.at.desc(), Event.id.desc())
            .limit(1)
        )
    ).first()
    last_check = None
    if last is not None:
        at = _as_utc(last.at)
        stale = now - at > timedelta(minutes=CHECK_STALE_FACTOR * config.CHECK_INTERVAL_MINUTES)
        ok = last.kind == ev.CHECK_OK
        last_check = {
            "at": _iso(at),
            "ok": ok,
            "new": int(last.ref) if ok and last.ref and last.ref.isdigit() else None,
            "attention": stale or not ok,
        }

    rows = await session.execute(
        select(Event.ref, func.count())
        .where(Event.kind == ev.ACT_PROCESSED, Event.at >= now - timedelta(hours=24))
        .group_by(Event.ref)
    )
    source = {"page": 0, "ocr": 0, "none": 0}
    outcome = {"ok": 0, "refused": 0, "error": 0, "none": 0}
    total = 0
    for ref, count in rows.all():
        src, _, out = (ref or "").partition("/")
        total += count
        if src in source:
            source[src] += count
        if out in outcome:
            outcome[out] += count
    return {"last_check": last_check, "acts_24h": {"total": total, "source": source, "outcome": outcome}}


async def _filters(session, users_ok) -> dict:
    """Сколько подписчиков выбрали каждый уровень и сколько не ограничивают уровни."""
    rows = await session.execute(
        select(UserFilter.level, func.count())
        .join(User, User.id == UserFilter.user_id)
        .where(users_ok, _is_subscriber())
        .group_by(UserFilter.level)
        .order_by(func.count().desc(), UserFilter.level)
    )
    unrestricted = await session.scalar(
        select(func.count())
        .select_from(User)
        .where(users_ok, _is_subscriber(), ~exists().where(UserFilter.user_id == User.id))
    ) or 0
    return {"levels": [{"level": level, "count": count} for level, count in rows.all()], "unrestricted": unrestricted}


def _describe(kind: str, ref: str | None) -> str:
    """Описание события для ленты на русском."""
    if kind == ev.USER_REGISTERED:
        return "зарегистрировался"
    if kind == ev.USER_VERIFIED:
        return "подтвердил подписку на канал"
    if kind == ev.USER_BLOCKED:
        return "⛔ заблокировал бота"
    if kind == ev.USER_UNBLOCKED:
        return "✅ разблокировал бота"
    if kind == ev.CMD:
        return "отправил неизвестную команду" if ref == "other" else f"команда /{ref}"
    if kind == ev.BUTTON:
        return f"нажал кнопку · {ref}"
    if kind == ev.SETTINGS_CHANGED:
        return "изменил фильтры"
    if kind == ev.WEBAPP_OPEN:
        return f"открыл Mini-App · {_WEBAPP_VIEWS.get(ref or '', ref or '')}"
    if kind == ev.SUMMARY_REQUESTED:
        return f"запросил саммари · {ref}"
    if kind == ev.SUMMARY_RESULT:
        return f"саммари по запросу · {_SUMMARY_STATUSES.get(ref or '', ref or '')}"
    if kind == ev.SUMMARY_LIMIT:
        return "упёрся в месячный лимит саммари"
    if kind == ev.NOTIFY_FAILED:
        return f"⚠️ уведомление не доставлено · {_NOTIFY_FAILURES.get(ref or '', ref or '')}"
    if kind == ev.CHECK_FAILED:
        return "⚠️ проверка pravo.gov.ru не удалась"
    if kind == ev.WEEKLY_SENT:
        return f"📌 недельная подборка отправлена · законов: {ref}"
    return kind


async def _feed(session, config) -> list[dict]:
    """Последние события без массовых видов: время по Москве, пользователь, описание, флаг админа."""
    rows = await session.execute(
        select(Event.at, Event.kind, Event.telegram_id, Event.ref, User.username)
        .outerjoin(User, User.telegram_id == Event.telegram_id)
        .where(Event.kind.notin_(FEED_EXCLUDED_KINDS))
        .order_by(Event.at.desc(), Event.id.desc())
        .limit(FEED_SIZE)
    )
    admins = set(config.ADMIN_CHAT_IDS)
    return [
        {
            "at": _iso(row.at),
            "time": _as_utc(row.at).astimezone(PORTAL_TZ).strftime("%d.%m %H:%M"),
            "user": (f"@{row.username}" if row.username else str(row.telegram_id)) if row.telegram_id else None,
            "text": _describe(row.kind, row.ref),
            "is_admin": row.telegram_id in admins,
        }
        for row in rows.all()
    ]


# -- Тренд ------------------------------------------------------------------

async def trend(session_maker, config, now: datetime | None = None) -> dict:
    """Данные вкладки «Тренд»: ряды по неделям за последние 12 недель, включая текущую.

    Регистрации, акты и подборка строятся по всей истории БД; ряды из журнала — ``None`` для
    недель, которые целиком раньше первого записанного события («нет данных», не ноль).
    """
    now = now or datetime.now(timezone.utc)
    this_monday = portal_today(now) - timedelta(days=portal_today(now).weekday())
    monday_dates = [this_monday - timedelta(weeks=TREND_WEEKS - 1 - i) for i in range(TREND_WEEKS)]
    origin = _msk_midnight(monday_dates[0])
    week_ends = [_msk_midnight(d + timedelta(weeks=1)) for d in monday_dates]
    admin_events = _not_admin(Event.telegram_id, config)

    async def weekly(session, table_column, *where, value=None) -> list[int]:
        """Значения по неделям: ``value`` (по умолчанию COUNT(*)) для строк с датой ``>= origin``."""
        bucket = _bucket(table_column, origin)
        rows = await session.execute(
            select(bucket, value if value is not None else func.count())
            .where(table_column >= origin, *where)
            .group_by(bucket)
        )
        result = [0] * TREND_WEEKS
        for index, count in rows.all():
            if 0 <= index < TREND_WEEKS:
                result[index] = count
        return result

    def events_where(*kinds, ref=None, system=False):
        """Условия по журналу; ``system=True`` — события без пользователя (администраторов не исключаем)."""
        conditions = [Event.kind.in_(kinds)]
        if ref is not None:
            conditions.append(Event.ref == ref)
        if not system:
            conditions.append(admin_events)
        return conditions

    async with session_maker() as session:
        since = await session.scalar(select(func.min(Event.at)))
        since_utc = _as_utc(since) if since else None

        registrations = await weekly(session, User.created_at, _not_admin(User.telegram_id, config))
        accepted = await weekly(session, Article.created_at, Article.is_demo.is_(False))
        with_summary = await weekly(
            session, Article.created_at, Article.is_demo.is_(False), Article.summary.isnot(None)
        )
        digest_runs = await weekly(session, WeeklyDigestRun.period_end, WeeklyDigestRun.finished_at.isnot(None))

        blocks = await weekly(session, Event.at, *events_where(ev.USER_BLOCKED))
        active = await weekly(
            session, Event.at, *events_where(*ACTIVE_KINDS), value=func.count(distinct(Event.telegram_id))
        )
        opens = await weekly(session, Event.at, *events_where(ev.WEBAPP_OPEN))
        summaries = await weekly(session, Event.at, *events_where(ev.SUMMARY_RESULT, ref="ok"))
        delivered = await weekly(session, Event.at, *events_where(ev.NOTIFY_SENT))
        check_failures = await weekly(session, Event.at, *events_where(ev.CHECK_FAILED, system=True))

    def from_journal(values: list[int]) -> list[int | None]:
        """Недели целиком до начала журнала — ``None`` вместо нуля."""
        return [
            None if since_utc is None or week_end <= since_utc else value
            for value, week_end in zip(values, week_ends)
        ]

    series = [
        {"key": "registrations", "title": "Регистрации", "values": registrations},
        {"key": "blocks", "title": "Заблокировали бота", "values": from_journal(blocks)},
        {"key": "active", "title": "Активные пользователи", "values": from_journal(active)},
        {"key": "opens", "title": "Открытия Mini-App", "values": from_journal(opens)},
        {"key": "summaries", "title": "Саммари по запросу", "values": from_journal(summaries)},
        {"key": "delivered", "title": "Доставлено уведомлений", "values": from_journal(delivered)},
        {"key": "accepted", "title": "Принято актов", "values": accepted},
        {"key": "with_summary", "title": "Из них с саммари", "values": with_summary},
        {"key": "check_failures", "title": "Сбои проверок pravo.gov.ru", "values": from_journal(check_failures)},
        {"key": "digest", "title": "Недельная подборка отправлена", "values": [min(v, 1) for v in digest_runs]},
    ]
    return {
        "generated_at": _iso(now),
        "tracking_since": _iso(since_utc) if since_utc else None,
        "weeks": [
            {"start": d.isoformat(), "current": d == this_monday} for d in monday_dates
        ],
        "series": series,
    }
