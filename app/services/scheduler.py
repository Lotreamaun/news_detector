"""
Периодическая проверка публикаций pravo.gov.ru, обработка новых законов и рассылка.

Запускается через PTB JobQueue (``run_repeating`` в ``app/main.py``) и работает
в том же event loop, что и бот. Сетевые вызовы (JSON API publication.pravo.gov.ru,
редакции actual.pravo.gov.ru, GigaChat) — асинхронные, не блокируют обработку
апдейтов.

Идемпотентность: документ обрабатывается и рассылается ровно один раз —
проверка по уникальному ``external_id`` (номер опубликования ``eoNumber``).
Если на момент обработки текст не готов и GigaChat недоступен, документ всё
равно сохраняется в БД, а пользователю уходит ссылка на оригинал (fallback
vision.md). OCR-фоллбэк через GigaChat запускается только для «важных» актов
(``is_important``, список пока пуст).
"""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from app.models import Article, User, UserFilter, WeeklyDigestRun, WeeklyReviewPing
from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError
from app.services.importance import (
    CANDIDATE_LEVELS,
    build_body,
    prefilter_zero,
    score_importance,
)
from app.services.rss_parser import (
    IMPORTANT_LEVELS,
    RssError,
    _normalize_text,
    classify_level,
    classify_level_for_title,
    fetch_day,
    fetch_documents,
    get_legal_text,
    is_important,
    ocr_document_text,
)
from app.services.summarizer import Summarizer, SummarizerConfig
from app.services.weekly_review import (
    EXCLUDE,
    INCLUDE,
    ADMIN_LINK_CALLBACK,
    WEEKLY_MIN_IMPORTANCE,
    TargetPeriod,
    final_selection,
    format_time_left,
    is_awaiting_rating,
    moderation_unavailable_reason,
    needs_summary_retry,
    review_counts,
    target_period,
)
from app.services.weekly_window import (
    DAY_END,
    SUNDAY_LAST_HOUR_START,
    in_daytime,
    last_digest_moment,
    previous_digest_moment,
    sunday_at,
)

logger = logging.getLogger(__name__)

# Префикс callback_data для кнопки «Сделать саммари» в одиночном уведомлении
# (правит существующее сообщение — уместно, когда сообщение об одном документе)
FORCE_SUMMARIZE_PREFIX = "force_sum:summary:"
# Префикс для той же кнопки внутри тизера дайджеста: результат уходит НОВЫМ
# сообщением (не правит текст тизера — иначе стёрлись бы остальные пункты)
DIGEST_SUMMARIZE_PREFIX = "force_sum:digest:"

# Мягкий технический потолок числа external_id в URL кнопки «Дайджест» — защита от
# переполнения ссылки в «громкий» день; документы сверх потолка доступны через /today
DIGEST_ID_CAP = 30

# Порядок значимости уровней внутри дайджеста (не входящих в IMPORTANT_LEVELS),
# от более значимого к менее — используется только для сортировки тизера/кнопки
_REST_LEVEL_ORDER = ("DECREE", "GOV_RESOLUTION", "DEPARTMENTAL", "REGIONAL")

# -- Оценка важности и недельная подборка -------------------------------------

# Добор неоценённых кандидатов: не старше стольких дней с приёма и не более N за цикл
IMPORTANCE_BACKLOG_DAYS = 14
IMPORTANCE_BACKLOG_LIMIT = 20
# Публикация старше стольких дней — явная история (бэкфилл), в добор не берём:
# покрывает текущее окно подборки и предыдущее, если его отправка отложена
IMPORTANCE_BACKLOG_PUBLISHED_DAYS = 21
# Нет текста дольше стольких часов после приёма — один OCR до оценки
OCR_AFTER_HOURS = 24

# Недельная подборка: порог оценки (WEEKLY_MIN_IMPORTANCE) и правила решений
# администратора живут в app/services/weekly_review.py
# Время рассылки для предпросмотра, пока WEEKLY_DIGEST_TIME не задан
WEEKLY_DEFAULT_TIME = time(18, 0)
# Допуск по дате публикации относительно начала окна (акты, опубликованные накануне
# окна, но принятые ботом уже в нём — простой бота, задержка портала)
WEEKLY_PUBLISHED_TOLERANCE_DAYS = 7
WEEKLY_HEADER_TEMPLATE = "📌 Еженедельная подборка: главные законы — {count}"


async def check_legislation_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк: проверяет API публикаций, обрабатывает новые законы."""
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]

    # бэкфилл: при старте, если в 30-дневном окне нет ФКЗ/ФЗ — загружаем их.
    # Флаг needs_backfill вычисляется один раз в _run_initial_check и сбрасывается
    # после первого прогона (не на каждый цикл), чтобы не дублировать запросы.
    if context.bot_data.get("needs_backfill"):
        logger.info("Бэкфилл: в окне 30 дней нет ФКЗ/ФЗ, загружаем")
        await _run_backfill(context)
        return

    try:
        entries = await fetch_documents(config.PRAVO_API_URL)
        new_entries = await _filter_new_entries(session_maker, entries)
    except RssError:
        logger.exception("Не удалось загрузить/разобрать API — пропускаем эту часть цикла")
        new_entries = []
    else:
        logger.info("В API %d документов, из них новых: %d", len(entries), len(new_entries))

    if new_entries:
        async with _llm_tools(config) as (summarizer, llm_client):
            for entry in new_entries:
                await _process_entry(context, session_maker, summarizer, llm_client, entry)

    # Рассылаем ВСЕ ещё не разосланные статьи, а не только сохранённые в этом
    # цикле — так подхватываются и «зависшие» после падения процесса между
    # сохранением и рассылкой (см. Article.notified).
    pending = await _fetch_pending_articles(session_maker)
    if pending:
        await _notify_users_batch(context, session_maker, pending)
        await _mark_notified(session_maker, [a.id for a in pending])

    # Сбой добора или недельной подборки не должен ломать основной цикл. Добор — после
    # мгновенных уведомлений, чтобы оценка накопившихся актов не задерживала новые.
    try:
        await _rate_backlog(context)
    except Exception:
        logger.exception("Ошибка добора неоценённых актов")
    await _maybe_send_review_ping(context)
    await _maybe_send_weekly_digest(context)


@asynccontextmanager
async def _llm_tools(config):
    """Открывает на время работы саммаризатор и клиент GigaChat (оценка, OCR); закрывает на выходе."""
    async with Summarizer(
        SummarizerConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            min_len=config.SUMMARY_MIN_LEN,
            max_len=config.SUMMARY_MAX_LEN,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    ) as summarizer, GigaChatClient(
        GigaChatConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    ) as llm_client:
        yield summarizer, llm_client


async def _run_backfill(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Одноразовый бэкфилл: 30 дней через fetch_day, сохраняет без рассылки и без LLM.

    Фильтруем на сервере только ФЗ и ФКЗ (documentTypes= повторяющимся
    параметром) — то, что нужно для дефолтного фильтра [Конституция,ФКЗ,ФЗ].
    """
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]

    from datetime import timedelta

    # GUID типов «ФЗ» и «ФКЗ» для серверного фильтра (без перебора регионов)
    from app.services.rss_parser import (
        _FKZ_DOCUMENT_TYPE_ID,
        _FZ_DOCUMENT_TYPE_ID,
    )

    type_ids = [_FZ_DOCUMENT_TYPE_ID, _FKZ_DOCUMENT_TYPE_ID]

    today = datetime.now(timezone.utc).date()
    stored = 0
    for offset in range(31):  # 30 дней назад включительно (календарные сутки)
        day = today - timedelta(days=offset)
        try:
            entries = await fetch_day(
                day, config.PRAVO_API_URL, document_type_ids=type_ids
            )
        except RssError as exc:
            logger.warning("Бэкфилл день %s не удался: %s, продолжаем", day, exc)
            continue
        for entry in entries:
            # notified=True: бэкфилл истории не должен рассылаться пользователям
            article = await _save_article(session_maker, entry, None, None, notified=True)
            if article is not None:
                stored += 1
        await asyncio.sleep(0.2)
    logger.info("Бэкфилл завершён: сохранено %d статей (ФЗ/ФКЗ) за 30 дней", stored)


# -- Внутреннее -------------------------------------------------------

async def _filter_new_entries(session_maker, entries: list) -> list:
    """Возвращает только те документы, которых ещё нет в БД (по external_id)."""
    ids = [entry.external_id for entry in entries]
    async with session_maker() as session:
        existing = set(
            (
                await session.scalars(
                    select(Article.external_id).where(Article.external_id.in_(ids))
                )
            ).all()
        )
    return [entry for entry in entries if entry.external_id not in existing]


async def _fetch_pending_articles(session_maker) -> list[Article]:
    """Возвращает все ещё не разосланные статьи (``notified=False``), не только новые за цикл."""
    async with session_maker() as session:
        return list(
            (await session.scalars(select(Article).where(Article.notified.is_(False)))).all()
        )


async def _mark_notified(session_maker, article_ids: list[int]) -> None:
    """Помечает статьи как разосланные — вызывается после успешного завершения рассылки."""
    if not article_ids:
        return
    async with session_maker() as session:
        await session.execute(
            update(Article).where(Article.id.in_(article_ids)).values(notified=True)
        )
        await session.commit()


async def _process_entry(
    context, session_maker, summarizer: Summarizer, llm_client: GigaChatClient, entry
) -> Article | None:
    """Обрабатывает один новый документ: текст -> саммари -> БД -> оценка важности.

    Рассылка — отдельным проходом. Оценка важности (кандидатам) идёт после сохранения в
    собственном ``try/except``: её сбой не мешает сохранению и рассылке.
    """
    logger.info("Обработка нового документа %s: %s", entry.external_id, entry.title)

    try:
        text = await get_legal_text(entry.external_id)
        if text:
            logger.info("Текст %s получен с actual.pravo.gov.ru", entry.external_id)
        elif is_important(entry):
            logger.info(
                "Текст %s не готов, документ «важный» — пробуем OCR через GigaChat",
                entry.external_id,
            )
            text = await ocr_document_text(entry.external_id, client=llm_client)
            if text:
                logger.info("OCR GigaChat вернул текст для %s", entry.external_id)
            else:
                logger.warning("OCR для %s не дал текста", entry.external_id)

        summary = None
        if text:
            try:
                summary = await summarizer.summarize(text)
            except GigaChatError as exc:
                logger.warning("Не удалось сделать саммари для %s: %s", entry.external_id, exc)

        article = await _save_article(session_maker, entry, text, summary)
    except Exception:
        logger.exception("Ошибка обработки документа %s", entry.external_id)
        return None

    if article is not None and article.level in CANDIDATE_LEVELS:
        try:
            # текст только что запрашивали; если он был, саммари уже пробовали сделать
            await _rate_article(
                context, session_maker, summarizer, llm_client, article,
                retry_text=False, summary_tried=bool(text),
            )
        except Exception:
            logger.exception("Ошибка оценки важности документа %s", entry.external_id)
    return article


async def _save_article(
    session_maker, entry, text: str | None, summary: str | None, *, notified: bool = False
) -> Article | None:
    """Сохраняет документ в БД; None, если запись уже существует (гонка).

    ``notified=True`` — только для бэкфилла: статья считается уже разосланной
    (точнее, никогда не подлежащей рассылке) сразу при сохранении.
    """
    level = classify_level_for_title(entry.title, entry.document_type_id)
    article = Article(
        external_id=entry.external_id,
        title=entry.title,
        original_text=text,
        summary=summary,
        url=entry.url,
        level=level,
        published_at=entry.published_at,
        notified=notified,
    )
    try:
        async with session_maker() as session:
            session.add(article)
            await session.commit()
            await session.refresh(article)
    except IntegrityError:
        logger.info("Документ %s уже сохранён (дубль) — пропускаем", entry.external_id)
        return None

    logger.info(
        "Сохранён документ %s (%s)",
        entry.external_id,
        "с саммари" if summary else "без саммари",
    )
    return article


# -- Оценка важности ----------------------------------------------------------

def _as_utc(value: datetime) -> datetime:
    """SQLite отдаёт даты без tzinfo (хранятся в UTC) — приводим к aware UTC."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def _update_article(session_maker, article_id: int, **values) -> None:
    """Обновляет поля статьи по id."""
    async with session_maker() as session:
        await session.execute(update(Article).where(Article.id == article_id).values(**values))
        await session.commit()


async def _rate_article(
    context,
    session_maker,
    summarizer: Summarizer,
    llm_client: GigaChatClient,
    article: Article,
    *,
    final: bool = False,
    retry_text: bool = True,
    summary_tried: bool = False,
) -> int | None:
    """Ставит акту-кандидату оценку важности (0–3) и сохраняет её; None — оценить не удалось.

    Сначала предфильтр по заголовку (``0`` без LLM). Вход оценки — название + саммари,
    иначе название + начало текста. Если текста нет, он запрашивается заново
    (``retry_text``); без текста дольше ``OCR_AFTER_HOURS`` с приёма один раз пробуем OCR.
    Оценка по одному названию допустима только на финальном проходе (``final``) или после
    неудачного OCR; иначе оценка откладывается до добора.

    После оценки ``>= 2`` акт без саммари саммаризуется в фоне (попытка 1), если это не
    делали раньше (``summary_tried``).
    """
    if prefilter_zero(article.level, article.title):
        await _update_article(session_maker, article.id, importance=0)
        article.importance = 0
        logger.info("Оценка %s: 0 (предфильтр по заголовку)", article.external_id)
        return 0

    text = article.original_text
    title_only_ok = final
    if not text and not article.summary and retry_text:
        text = await get_legal_text(article.external_id)
        if text:
            await _update_article(session_maker, article.id, original_text=text)
            article.original_text = text
        else:
            ocr_tried: set[str] = context.bot_data.setdefault("ocr_tried", set())
            created_at = _as_utc(article.created_at) if article.created_at else datetime.now(timezone.utc)
            if article.external_id in ocr_tried:
                title_only_ok = True  # OCR уже пробовали в этом процессе и текста нет
            elif datetime.now(timezone.utc) - created_at >= timedelta(hours=OCR_AFTER_HOURS):
                ocr_tried.add(article.external_id)
                logger.info("Текста %s нет больше %d ч — пробуем OCR до оценки", article.external_id, OCR_AFTER_HOURS)
                text = await ocr_document_text(article.external_id, client=llm_client)
                if text:
                    await _update_article(session_maker, article.id, original_text=text)
                    article.original_text = text
                else:
                    title_only_ok = True

    body = build_body(article.summary, text)
    if body is None and not title_only_ok:
        logger.debug("Оценка %s отложена: нет ни саммари, ни текста", article.external_id)
        return None

    score = await score_importance(llm_client, article.title, body)
    if score is None:
        return None
    await _update_article(session_maker, article.id, importance=score)
    article.importance = score
    logger.info(
        "Оценка %s: %d (вход: %s)", article.external_id, score, "название + текст" if body else "название"
    )

    if score >= WEEKLY_MIN_IMPORTANCE and not article.summary and not summary_tried:
        await _summarize_in_background(context, session_maker, summarizer, llm_client, article)
    return score


async def _summarize_in_background(
    context, session_maker, summarizer: Summarizer, llm_client: GigaChatClient, article: Article
) -> None:
    """Фоновая саммаризация важного акта: текст (при отсутствии — OCR) -> саммари -> БД.

    Отказ или сбой ничего не пишет в ``Article.summary``: заглушка подставляется только
    при сборке недельного сообщения, иначе она стала бы «кэшированным саммари» для
    /summary и кнопок. Расход лимитов пользователей не затрагивается.
    """
    text = article.original_text
    if not text:
        text = await get_legal_text(article.external_id)
        if not text:
            ocr_tried: set[str] = context.bot_data.setdefault("ocr_tried", set())
            if article.external_id not in ocr_tried:
                ocr_tried.add(article.external_id)
                text = await ocr_document_text(article.external_id, client=llm_client)
        if text:
            await _update_article(session_maker, article.id, original_text=text)
            article.original_text = text
    if not text:
        logger.info("Фоновое саммари %s: текста нет", article.external_id)
        return
    try:
        summary = await summarizer.summarize(text)
    except GigaChatError as exc:
        logger.warning("Фоновое саммари %s не получено: %s", article.external_id, exc)
        return
    await _update_article(session_maker, article.id, summary=summary)
    article.summary = summary
    logger.info("Фоновое саммари %s сохранено", article.external_id)


async def _rate_backlog(context) -> None:
    """Добор: оценивает кандидатов без оценки, принятых не позже ``IMPORTANCE_BACKLOG_DAYS`` назад.

    Не более ``IMPORTANCE_BACKLOG_LIMIT`` за цикл, старые раньше. Кандидаты без текста и
    саммари до суток (и до OCR) пропускаются без вызова LLM — оценка дождётся текста.
    """
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]
    now = datetime.now(timezone.utc)
    async with session_maker() as session:
        articles = list(
            (
                await session.scalars(
                    select(Article)
                    .where(
                        Article.importance.is_(None),
                        Article.level.in_(CANDIDATE_LEVELS),
                        Article.is_demo.is_(False),
                        Article.created_at >= now - timedelta(days=IMPORTANCE_BACKLOG_DAYS),
                        Article.published_at >= now - timedelta(days=IMPORTANCE_BACKLOG_PUBLISHED_DAYS),
                    )
                    .order_by(Article.created_at)
                    .limit(IMPORTANCE_BACKLOG_LIMIT)
                )
            ).all()
        )
    if not articles:
        return
    logger.info("Добор оценок: %d неоценённых кандидатов", len(articles))
    async with _llm_tools(config) as (summarizer, llm_client):
        for article in articles:
            try:
                await _rate_article(context, session_maker, summarizer, llm_client, article)
            except Exception:
                logger.exception("Ошибка добора оценки для %s", article.external_id)


# -- Недельная подборка ------------------------------------------------------

async def weekly_digest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк пятничной задачи: отправляет недельную подборку (если ещё не уходила)."""
    await _maybe_send_weekly_digest(context)


async def _load_weekly_window(session_maker, start: datetime, end: datetime) -> list[Article]:
    """Кандидаты окна подборки: приняты в ``(start, end]``, не демо, опубликованы не раньше
    ``start`` минус допуск (отсекает явную историю, например бэкфилл)."""
    published_from = datetime.combine(
        start.date() - timedelta(days=WEEKLY_PUBLISHED_TOLERANCE_DAYS), time.min, tzinfo=timezone.utc
    )
    async with session_maker() as session:
        return list(
            (
                await session.scalars(
                    select(Article).where(
                        Article.level.in_(CANDIDATE_LEVELS),
                        Article.is_demo.is_(False),
                        Article.created_at > _as_utc(start),
                        Article.created_at <= _as_utc(end),
                        Article.published_at >= published_from,
                    )
                )
            ).all()
        )


def _build_weekly_message(articles: list[Article], webapp_url: str | None) -> tuple[str, InlineKeyboardMarkup]:
    """Недельное сообщение: тизер дайджеста с отдельным заголовком и порядком по важности."""
    order = {a.id: i for i, a in enumerate(articles)}
    return _build_digest(
        articles,
        webapp_url,
        header=WEEKLY_HEADER_TEMPLATE.format(count=len(articles)),
        sort_key=lambda a: order[a.id],
    )


async def build_weekly_preview(session_maker, config) -> tuple[str | None, InlineKeyboardMarkup | None, str]:
    """Предпросмотр для /test_digest weekly: подборка за текущее окно + сводка для администратора.

    Окно — от последнего запланированного момента до «сейчас» (то, что накапливается к
    следующей рассылке). Ничего не пишет в БД и не запускает оценку/саммаризацию.

    Returns:
        ``(текст MarkdownV2, клавиатура, сводка)``; текст и клавиатура ``None``, если подборка пуста.
    """
    tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)
    now = datetime.now(timezone.utc)
    start = last_digest_moment(now, config.WEEKLY_DIGEST_TIME or WEEKLY_DEFAULT_TIME, tz)
    window = await _load_weekly_window(session_maker, start, now)
    included = final_selection(window)
    without_summary = [a for a in window if needs_summary_retry(a)]
    unrated = [a for a in window if is_awaiting_rating(a)]
    forced = [a for a in window if a.digest_override == INCLUDE]
    dropped = [a for a in window if a.digest_override == EXCLUDE]
    zeros = [a for a in window if a.importance == 0]
    prefiltered = sum(1 for a in zeros if prefilter_zero(a.level, a.title))
    ones = sum(1 for a in window if a.importance == 1)

    lines = [
        f"[test_digest weekly] Окно: {start.strftime('%d.%m %H:%M')} — "
        f"{now.astimezone(tz).strftime('%d.%m %H:%M')} ({config.WEEKLY_DIGEST_TZ})",
        f"Включено в подборку: {len(included)}",
    ]
    lines += [f"  {a.external_id} — оценка {a.importance}" for a in included[:DIGEST_ID_CAP]]
    lines.append(f"Включено администратором: {len(forced)}")
    lines += [f"  {a.external_id} — оценка {a.importance}" for a in forced[:DIGEST_ID_CAP]]
    lines.append(f"Исключено администратором: {len(dropped)}")
    lines += [f"  {a.external_id} — оценка {a.importance}" for a in dropped[:DIGEST_ID_CAP]]
    lines.append(
        f"Без саммари (будут исключены, если саммари не появится до отправки): {len(without_summary)}"
    )
    lines += [f"  {a.external_id} — оценка {a.importance}" for a in without_summary[:DIGEST_ID_CAP]]
    lines.append(f"Неоценённые кандидаты: {len(unrated)}")
    lines += [f"  {a.external_id}" for a in unrated[:DIGEST_ID_CAP]]
    lines.append(
        f"Отсеяно предфильтром: {prefiltered}; оценено 0: {len(zeros) - prefiltered}; оценено 1: {ones}"
    )
    if not included:
        return None, None, "\n".join(lines)
    text, markup = _build_weekly_message(included, config.WEBAPP_URL)
    return text, markup, "\n".join(lines)


async def _weekly_run_finished(session_maker, period_end: datetime) -> bool:
    """True, если рассылка за период уже завершена."""
    async with session_maker() as session:
        finished = await session.scalar(
            select(WeeklyDigestRun.finished_at).where(WeeklyDigestRun.period_end == _as_utc(period_end))
        )
    return finished is not None


async def _weekly_run_start(session_maker, period_end: datetime) -> None:
    """Фиксирует начало рассылки периода (повторный старт после сбоя обновляет ``started_at``)."""
    async with session_maker() as session:
        run = await session.scalar(
            select(WeeklyDigestRun).where(WeeklyDigestRun.period_end == _as_utc(period_end))
        )
        if run is None:
            session.add(WeeklyDigestRun(period_end=_as_utc(period_end), started_at=datetime.now(timezone.utc)))
        else:
            run.started_at = datetime.now(timezone.utc)
        await session.commit()


async def _weekly_run_finish(session_maker, period_end: datetime) -> None:
    """Фиксирует завершение рассылки периода."""
    async with session_maker() as session:
        await session.execute(
            update(WeeklyDigestRun)
            .where(WeeklyDigestRun.period_end == _as_utc(period_end))
            .values(finished_at=datetime.now(timezone.utc))
        )
        await session.commit()


async def load_review_target(
    session_maker, config, now: datetime | None = None
) -> tuple[TargetPeriod, list[Article]]:
    """Целевой период панели модерации и его окно (design.md, п. 5); ничего не пишет в БД."""
    now = now or datetime.now(timezone.utc)
    tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)
    last = last_digest_moment(now, config.WEEKLY_DIGEST_TIME, tz)
    async with session_maker() as session:
        run = await session.scalar(
            select(WeeklyDigestRun).where(WeeklyDigestRun.period_end == _as_utc(last))
        )
    target = target_period(
        now,
        config.WEEKLY_DIGEST_TIME,
        tz,
        run.started_at if run else None,
        run.finished_at if run else None,
    )
    window = await _load_weekly_window(session_maker, target.window_start, target.window_end)
    return target, window


async def weekly_review_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк пятничной задачи: напоминание администраторам (если ещё не отправлялось)."""
    await _maybe_send_review_ping(context)


async def _maybe_send_review_ping(context) -> None:
    """Единая точка отправки напоминания администраторам; безопасна при любом числе вызовов.

    Вызывается пятничной задачей, из ``_post_init`` и в конце каждого цикла проверки.
    Условия: модерация доступна, напоминание включено, сейчас — пятница не раньше
    ``WEEKLY_REVIEW_TIME`` и раньше запланированного момента, период не отправляется и
    отметки в ``weekly_review_pings`` ещё нет. Сбой логируется и не пробрасывается.
    """
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]
    if config.WEEKLY_REVIEW_TIME is None or moderation_unavailable_reason(config):
        return
    lock: asyncio.Lock = context.bot_data.setdefault("weekly_review_lock", asyncio.Lock())
    async with lock:
        try:
            now = datetime.now(timezone.utc)
            tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)
            target, window = await load_review_target(session_maker, config, now)
            local = now.astimezone(tz)
            if (
                target.sending
                or now >= target.period_end
                or local.date() != target.period_end.date()
                or local.time() < config.WEEKLY_REVIEW_TIME
            ):
                return
            # отметка ставится ДО отправки: напоминание не должно прийти дважды ни при каких сбоях
            async with session_maker() as session:
                session.add(WeeklyReviewPing(period_end=_as_utc(target.period_end), pinged_at=now))
                try:
                    await session.commit()
                except IntegrityError:
                    return
            await _send_review_ping(context, config, review_counts(window), target.period_end - now, target.period_end)
        except Exception:
            logger.exception("Ошибка отправки напоминания администраторам")


def build_review_ping(
    config, counts, time_left: timedelta, period_end: datetime
) -> tuple[str, InlineKeyboardMarkup]:
    """Текст и кнопки служебного напоминания администратору (получателей не знает)."""
    tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)
    when = period_end.astimezone(tz).strftime("%H:%M")
    text = (
        "🛠 Служебное сообщение для администратора\n\n"
        f"Недельная подборка уйдёт сегодня в {when} (через {format_time_left(time_left)}).\n"
        f"• В подборке: {counts.included}\n"
        f"• Важных без саммари: {counts.no_summary}\n"
        f"• Ещё не оценено: {counts.unrated}\n\n"
        "Проверьте состав и саммари в панели — это пара минут. "
        "Не успеете — подборка уйдёт автоматически."
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Открыть панель", web_app=WebAppInfo(url=f"{config.WEBAPP_URL.rstrip('/')}/admin"))],
            [InlineKeyboardButton("🔗 Ссылка для браузера", callback_data=ADMIN_LINK_CALLBACK)],
        ]
    )
    return text, markup


async def build_review_ping_now(session_maker, config) -> tuple[str, InlineKeyboardMarkup]:
    """Напоминание с числами на момент вызова — для ``/test_digest review`` (ничего не пишет в БД)."""
    now = datetime.now(timezone.utc)
    target, window = await load_review_target(session_maker, config, now)
    return build_review_ping(config, review_counts(window), target.period_end - now, target.period_end)


async def _send_review_ping(context, config, counts, time_left: timedelta, period_end: datetime) -> None:
    """Служебное напоминание: ТОЛЬКО администраторам (``ADMIN_CHAT_IDS``), напрямую через Bot API.

    Намеренно не читает ``users`` и не использует ``_send_notification`` (тот деактивирует
    пользователей по ``Forbidden``): получатели не принимаются извне, поэтому напоминание
    не может попасть подписчикам. Ошибка у одного администратора не мешает остальным.
    """
    text, markup = build_review_ping(config, counts, time_left, period_end)
    sent = 0
    for chat_id in config.ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            sent += 1
        except Exception:
            logger.warning("Напоминание администратору %s не отправлено", chat_id, exc_info=True)
    logger.info("Напоминание о модерации подборки за %s: отправлено %d из %d администраторов",
                period_end, sent, len(config.ADMIN_CHAT_IDS))


async def _maybe_send_weekly_digest(context) -> None:
    """Единая точка отправки недельной подборки; безопасна при любом числе вызовов.

    Вызывается из пятничной задачи, из ``_post_init`` (догон после простоя) и в конце
    каждого цикла проверки (повтор отложенной отправки). Под ``asyncio.Lock``, чтобы
    задача и цикл не отправили одновременно; сбой логируется и не пробрасывается.
    """
    if context.bot_data["config"].WEEKLY_DIGEST_TIME is None:
        return
    lock: asyncio.Lock = context.bot_data.setdefault("weekly_digest_lock", asyncio.Lock())
    async with lock:
        try:
            await _send_weekly_digest(context)
        except Exception:
            logger.exception("Ошибка отправки недельной подборки")


async def _send_weekly_digest(context) -> None:
    """Проверки и отправка недельной подборки (порядок — design.md, п. 11); вызывать под локом."""
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]
    tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)

    now = datetime.now(timezone.utc)
    period_end = last_digest_moment(now, config.WEEKLY_DIGEST_TIME, tz)
    period_start = previous_digest_moment(period_end)

    if await _weekly_run_finished(session_maker, period_end):
        return
    if now > sunday_at(period_end, DAY_END):
        skipped: set[datetime] = context.bot_data.setdefault("weekly_skipped", set())
        if period_end not in skipped:
            skipped.add(period_end)
            logger.warning("Недельная подборка за %s пропущена: срок (воскресенье %s) прошёл", period_end, DAY_END)
        return
    if not in_daytime(now, tz):
        return

    window = await _load_weekly_window(session_maker, period_start, period_end)
    if any(is_awaiting_rating(a) or needs_summary_retry(a) for a in window):
        async with _llm_tools(config) as (summarizer, llm_client):
            # финальный проход: оцениваем всех оставшихся, в т.ч. по названию; документы
            # с решением администратора не оцениваем (is_awaiting_rating их пропускает)
            for article in [a for a in window if is_awaiting_rating(a)]:
                try:
                    await _rate_article(context, session_maker, summarizer, llm_client, article, final=True)
                except Exception:
                    logger.exception("Ошибка финальной оценки %s", article.external_id)
            window = await _load_weekly_window(session_maker, period_start, period_end)
            unrated = [a for a in window if is_awaiting_rating(a)]
            if unrated:
                if datetime.now(timezone.utc) < sunday_at(period_end, SUNDAY_LAST_HOUR_START):
                    logger.info("Недельная подборка отложена: неоценённых кандидатов %d", len(unrated))
                    return
                logger.warning(
                    "Недельная подборка уходит без неоценённых: %s", ", ".join(a.external_id for a in unrated)
                )
            # попытка 2 фоновой саммаризации — только для тех, кто реально пойдёт (не исключён)
            for article in [a for a in window if needs_summary_retry(a)]:
                try:
                    await _summarize_in_background(context, session_maker, summarizer, llm_client, article)
                except Exception:
                    logger.exception("Ошибка финальной саммаризации %s", article.external_id)

    included = final_selection(window)
    without_summary = [a for a in window if needs_summary_retry(a)]
    if without_summary:
        # в подборку идёт только то, что можно прочитать (design п. 9)
        logger.warning(
            "Недельная подборка за %s: исключены без саммари: %s",
            period_end, ", ".join(a.external_id for a in without_summary),
        )
    if not included:
        await _weekly_run_start(session_maker, period_end)
        await _weekly_run_finish(session_maker, period_end)
        logger.info("Недельная подборка за %s пуста — ничего не отправляем", period_end)
        return

    text, markup = _build_weekly_message(included, config.WEBAPP_URL)
    await _weekly_run_start(session_maker, period_end)
    async with session_maker() as session:
        users = list(
            (
                await session.scalars(
                    select(User).where(User.is_active.is_(True), User.channel_verified.is_(True))
                )
            ).all()
        )
    sent = 0
    for user in users:
        # фильтры пользователя не применяются: подборка одинакова для всех
        try:
            if await _send_notification(context, session_maker, user, text, markup):
                sent += 1
        except Exception:
            logger.exception("Ошибка отправки подборки пользователю %s — пропуск", user.telegram_id)
        await asyncio.sleep(0.05)
    await _weekly_run_finish(session_maker, period_end)
    logger.info("Недельная подборка за %s: %d законов, отправлена %d из %d пользователей",
                period_end, len(included), sent, len(users))


async def _notify_users_batch(context, session_maker, articles: list[Article]) -> None:
    """Рассылает уведомления о всех ``articles``, ожидающих рассылки, каждому пользователю.

    ``articles`` — не обязательно только новые документы текущего цикла: сюда же
    попадают статьи, «зависшие» с прошлого цикла из-за сбоя между сохранением и
    рассылкой (см. ``Article.notified`` и ``_fetch_pending_articles``).
    Для каждого пользователя список фильтруется по его ``UserFilter``, затем делится
    на важные (``IMPORTANT_LEVELS``) и остальные. Важные доставляются отдельным
    индивидуальным сообщением каждая (``_build_notification``), независимо от их
    числа. Остальные: 1 подходящий — тоже индивидуальное сообщение, 2+ — один
    тизер дайджеста (``_build_digest``) с кнопкой «Дайджест».
    """
    # на первом прогоне после деплоя не спамим старыми законами
    if context.bot_data.get("is_first_run"):
        logger.info("is_first_run — пропуск рассылки для %d документов (только сохранение)", len(articles))
        return

    async with session_maker() as session:
        users = (
            await session.scalars(
                select(User).where(User.is_active.is_(True), User.channel_verified.is_(True))
            )
        ).all()

        if not users:
            logger.debug("Активных пользователей для рассылки нет")
            return

        # фильтры всех пользователей одним запросом вместо запроса на пользователя
        filters_by_user: dict[int, set[str]] = {}
        try:
            rows = await session.execute(
                select(UserFilter.user_id, UserFilter.level).where(
                    UserFilter.user_id.in_([u.id for u in users])
                )
            )
            for user_id, level in rows.all():
                filters_by_user.setdefault(user_id, set()).add(level)
        except Exception:
            logger.exception("Ошибка загрузки фильтров пользователей — рассылаем всем без фильтра")
            filters_by_user = {}

    webapp_url = context.bot_data["config"].WEBAPP_URL
    sent = 0
    for user in users:
        # фильтр по силе: пусто = Все, дефолт [Конституция,FKZ,FZ] уже в БД, но на всякий — пусто = Все
        # для нового юзера без настройки дефолт уже записан как 3 уровня, так что пусто действительно значит Все
        levels = filters_by_user.get(user.id, set())
        if levels:
            # UNKNOWN только для Все (пусто); дефолт [Конституция,FKZ,FZ] — не пусто, проверяем вхождение
            matched = [a for a in articles if a.level != "UNKNOWN" and a.level in levels]
        else:
            matched = list(articles)

        if not matched:
            logger.debug("Пропуск рассылки для %s: нет подходящих документов", user.telegram_id)
            continue

        try:
            important = [a for a in matched if a.level in IMPORTANT_LEVELS]
            rest = [a for a in matched if a.level not in IMPORTANT_LEVELS]

            messages: list[tuple[str, InlineKeyboardMarkup]] = [
                _build_notification(a, webapp_url) for a in important
            ]
            if len(rest) == 1:
                messages.append(_build_notification(rest[0], webapp_url))
            elif len(rest) >= 2:
                messages.append(_build_digest(rest, webapp_url))

            user_sent = False
            for message, reply_markup in messages:
                result = await _send_notification(context, session_maker, user, message, reply_markup)
                if result is None:
                    # пользователь заблокировал бота и деактивирован — дальше слать нечего
                    break
                if result:
                    user_sent = True
                # анти-спам пауза между отправками (и между пользователями)
                await asyncio.sleep(0.05)

            if user_sent:
                sent += 1
        except Exception:
            # Не даём сбою на одном пользователе (например, при построении сообщения)
            # прервать рассылку остальным
            logger.exception("Ошибка рассылки пользователю %s — пропуск", user.telegram_id)
            continue

    logger.info("Уведомления отправлены %d из %d пользователей", sent, len(users))


async def _send_notification(context, session_maker, user: User, message: str, reply_markup) -> bool | None:
    """Отправляет одно сообщение пользователю с обработкой лимита 429 и блокировки 403.

    Возвращает True, если сообщение в итоге доставлено, False при обычной неудаче,
    None — если пользователь заблокировал бота (деактивирован, дальнейшая отправка
    ему в этом цикле бессмысленна).
    """
    try:
        await context.bot.send_message(
            chat_id=user.telegram_id,
            text=message,
            reply_markup=reply_markup,
            parse_mode="MarkdownV2",
        )
        return True
    except Exception as exc:
        msg = str(exc).lower()
        if "retry after" in msg or "too many requests" in msg or "429" in msg:
            # парсим RetryAfter если есть
            retry_after = 2
            try:
                m = re.search(r"retry after (\d+)", msg)
                if m:
                    retry_after = int(m.group(1)) + 1
            except Exception:
                pass
            logger.warning("429 для %s, sleep %sс", user.telegram_id, retry_after)
            await asyncio.sleep(retry_after)
            try:
                await context.bot.send_message(
                    chat_id=user.telegram_id,
                    text=message,
                    reply_markup=reply_markup,
                    parse_mode="MarkdownV2",
                )
                return True
            except Exception:
                logger.warning(
                    "Повторная отправка не удалась для %s",
                    user.telegram_id,
                    exc_info=True,
                )
                return False
        elif "forbidden" in msg or "blocked" in msg or "403" in msg:
            logger.warning("403 для %s — деактивируем", user.telegram_id)
            try:
                async with session_maker() as s3:
                    u = await s3.scalar(select(User).where(User.telegram_id == user.telegram_id))
                    if u:
                        u.is_active = False
                        await s3.commit()
            except Exception:
                logger.exception("Не удалось деактивировать %s", user.telegram_id)
            return None
        else:
            logger.warning(
                "Не удалось отправить уведомление пользователю %s",
                user.telegram_id,
                exc_info=True,
            )
            return False


def _build_notification(
    article: Article, webapp_url: str | None = None
) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует MarkdownV2-текст уведомления и кнопки.

    Добавляет кнопку «Полный текст» (Mini-App), если передан webapp_url.
    """
    title_clean = _normalize_text(article.title) or article.title
    title_esc = escape_markdown(title_clean, version=2)
    if article.summary:
        summary_clean = _normalize_text(article.summary) or article.summary
        summary_esc = escape_markdown(summary_clean, version=2)
        body = f"*{title_esc}*\n\n{summary_esc}"
    else:
        # Fallback (vision.md): GigaChat недоступен или нет текста — ссылка на оригинал
        fallback = escape_markdown(
            "Содержание этого закона пока не доступно в текстовом формате. "
            "Откройте оригинал на портале или запросите саммари через бота.",
            version=2,
        )
        body = f"*{title_esc}*\n\n_{fallback}_"
    url_esc = escape_markdown(article.url, version=2)
    text = f"{body}\n\n[Читать на портале]({url_esc})"

    buttons: list[list[InlineKeyboardButton]] = []
    # Кнопка «Сделать саммари» только если саммари ещё нет
    if not article.summary:
        buttons.append(
            [
                InlineKeyboardButton(
                    "Сделать саммари",
                    callback_data=f"{FORCE_SUMMARIZE_PREFIX}{article.external_id}",
                )
            ]
        )
    # Кнопка «Полный текст» только после саммари (важный закон — по умолчанию, иначе после принудительной)
    if webapp_url and article.summary:
        buttons.append(
            [
                InlineKeyboardButton(
                    "📖 Полный текст",
                    web_app=WebAppInfo(
                        url=f"{webapp_url.rstrip('/')}/app?external_id={article.external_id}"
                    ),
                )
            ]
        )
    # Если кнопок нет (саммари есть, но webapp не настроен) — вернуть пустую разметку
    if not buttons:
        return text, InlineKeyboardMarkup([])
    return text, InlineKeyboardMarkup(buttons)


def _digest_sort_key(article: Article) -> tuple[int, str]:
    """Ключ сортировки тизера: по значимости уровня, затем по алфавиту заголовка."""
    rank = (
        _REST_LEVEL_ORDER.index(article.level)
        if article.level in _REST_LEVEL_ORDER
        else len(_REST_LEVEL_ORDER)
    )
    title = _normalize_text(article.title) or article.title or ""
    return (rank, title)


def _build_digest(
    articles: list[Article],
    webapp_url: str | None = None,
    *,
    header: str | None = None,
    sort_key: Callable[[Article], object] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Формирует MarkdownV2-тизер дайджеста: до 3 самых значимых документов + кнопка «Дайджест».

    Документы группы сортируются по значимости уровня (``_REST_LEVEL_ORDER``), при
    равенстве — по алфавиту заголовка. Показывает не больше 3 первых по этому
    порядку (все, если их не больше 3), пронумерованных (1., 2., ...) — полный
    заголовок, саммари при наличии, ссылка на портал — плюс общее число документов
    дайджеста. Кнопка «Дайджест» открывает WebApp с документами группы в том же
    порядке, до ``DIGEST_ID_CAP`` штук (остаток доступен через /today) — благодаря
    общей сортировке документы тизера всегда попадают и в список кнопки; без
    ``webapp_url`` нет ни кнопки, ни подсказки. Саммари недостающих документов
    делается прямо в Mini-App, поэтому кнопок «Сделать саммари №N» тизер больше не
    содержит (они остались только в ранее отправленных дайджестах, см.
    ``force_summarize_digest``) — вместо них при заданном ``webapp_url`` в текст
    добавляется подсказка про дайджест.

    Необязательные параметры для недельной подборки (по умолчанию — поведение дневного
    дайджеста): ``header`` — свой заголовок, ``sort_key`` — свой порядок.
    """
    ordered = sorted(articles, key=sort_key or _digest_sort_key)
    header_esc = escape_markdown(header or f"Приняли новые законы — {len(ordered)}", version=2)
    lines: list[str] = [f"*{header_esc}*"]

    sample = ordered[:3]
    for i, article in enumerate(sample, 1):
        title_clean = _normalize_text(article.title) or article.title
        title_esc = escape_markdown(f"{i}. {title_clean}", version=2)
        url_esc = escape_markdown(article.url, version=2)
        if article.summary:
            summary_clean = _normalize_text(article.summary) or article.summary
            summary_esc = escape_markdown(summary_clean, version=2)
            block = f"*{title_esc}*\n{summary_esc}\n[Читать на портале]({url_esc})"
        else:
            block = f"*{title_esc}*\n[Читать на портале]({url_esc})"
        lines.append(block)

    if webapp_url:
        lines.append(
            escape_markdown(
                "Саммари и полные тексты — в дайджесте по кнопке ниже. "
                "Там же можно сделать саммари для любого закона",
                version=2,
            )
        )

    text = "\n\n".join(lines)

    buttons: list[list[InlineKeyboardButton]] = []
    if webapp_url:
        capped = ordered[:DIGEST_ID_CAP]
        ids_param = ",".join(quote(a.external_id, safe="") for a in capped)
        buttons.append(
            [
                InlineKeyboardButton(
                    "📖 Дайджест",
                    web_app=WebAppInfo(url=f"{webapp_url.rstrip('/')}/app?ids={ids_param}"),
                )
            ]
        )

    return text, InlineKeyboardMarkup(buttons)
