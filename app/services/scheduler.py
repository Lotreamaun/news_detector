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
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

from app.models import Article, User
from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError
from app.services.rss_parser import (
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

logger = logging.getLogger(__name__)

# Префикс callback_data для кнопки «Сделать саммари»
FORCE_SUMMARIZE_PREFIX = "force_sum:summary:"




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
    except RssError:
        logger.exception("Проверка отменена: не удалось загрузить/разобрать API")
        return

    new_entries = await _filter_new_entries(session_maker, entries)
    if not new_entries:
        logger.info("В API %d документов, новых нет — рассылка не требуется", len(entries))
        return
    logger.info("В API %d документов, из них новых: %d", len(entries), len(new_entries))

    async with Summarizer(
        SummarizerConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            min_len=config.SUMMARY_MIN_LEN,
            max_len=config.SUMMARY_MAX_LEN,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    ) as summarizer:
        for entry in new_entries:
            await _process_entry(context, session_maker, summarizer, entry)


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
            article = await _save_article(session_maker, entry, None, None)
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


async def _process_entry(context, session_maker, summarizer: Summarizer, entry) -> None:
    """Обрабатывает один новый документ: текст -> саммари -> БД -> рассылка."""
    logger.info("Обработка нового документа %s: %s", entry.external_id, entry.title)

    try:
        config = context.bot_data["config"]

        text = await get_legal_text(entry.external_id)
        if text:
            logger.info("Текст %s получен с actual.pravo.gov.ru", entry.external_id)
        elif is_important(entry):
            logger.info(
                "Текст %s не готов, документ «важный» — пробуем OCR через GigaChat",
                entry.external_id,
            )
            async with GigaChatClient(
                GigaChatConfig(
                    auth_key=config.GIGACHAT_AUTH_KEY,
                    verify_ssl=config.GIGACHAT_VERIFY_SSL,
                )
            ) as ocr_client:
                text = await ocr_document_text(entry.external_id, client=ocr_client)
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
        if article is None:
            return  # дубль (гонка) — документ уже сохранил другой запуск

        await _notify_users(context, session_maker, article)
    except Exception:
        logger.exception("Ошибка обработки документа %s", entry.external_id)


async def _save_article(session_maker, entry, text: str | None, summary: str | None) -> Article | None:
    """Сохраняет документ в БД; None, если запись уже существует (гонка)."""
    level = classify_level_for_title(entry.title, entry.document_type_id)
    article = Article(
        external_id=entry.external_id,
        title=entry.title,
        original_text=text,
        summary=summary,
        url=entry.url,
        level=level,
        published_at=entry.published_at,
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


async def _notify_users(context, session_maker, article: Article) -> None:
    """Рассылает уведомление о документе всем активным пользователям с учётом фильтра по силе."""
    # на первом прогоне после деплоя не спамим старыми законами
    if context.bot_data.get("is_first_run"):
        logger.info("is_first_run — пропуск рассылки для %s (только сохранение)", article.external_id)
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

    message, reply_markup = _build_notification(
        article, context.bot_data["config"].WEBAPP_URL
    )
    sent = 0
    for user in users:
        # фильтр по силе: пусто = Все, дефолт [Конституция,FKZ,FZ] уже в БД, но на всякий — пусто = Все
        # для нового юзера без настройки дефолт уже записан как 3 уровня, так что пусто действительно значит Все
        try:
            from app.models.user_filter import UserFilter

            async with session_maker() as s2:
                lvls = await s2.scalars(select(UserFilter.level).where(UserFilter.user_id == user.id))
                levels = set(lvls.all())
            if levels:
                # UNKNOWN только для Все (пусто)
                if article.level == "UNKNOWN":
                    logger.debug("Пропуск %s для %s: UNKNOWN только для Все", article.external_id, user.telegram_id)
                    continue
                # дефолт [Конституция,FKZ,FZ] — это не пусто, проверяем вхождение
                if article.level not in levels:
                    logger.debug(
                        "Пропуск %s для %s: фильтр %s не содержит %s",
                        article.external_id,
                        user.telegram_id,
                        levels,
                        article.level,
                    )
                    continue
        except Exception:
            logger.exception("Ошибка проверки фильтра для %s", user.telegram_id)
            # при ошибке — отправляем как раньше (fail open)
        try:
            await context.bot.send_message(
                chat_id=user.telegram_id,
                text=message,
                reply_markup=reply_markup,
                parse_mode="MarkdownV2",
            )
            sent += 1
            # анти-спам пауза между пользователями
            await asyncio.sleep(0.05)
        except Exception as exc:
            # обработка Telegram лимитов
            msg = str(exc).lower()
            if "retry after" in msg or "too many requests" in msg or "429" in msg:
                # парсим RetryAfter если есть
                retry_after = 2
                try:
                    import re

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
                    sent += 1
                except Exception:
                    logger.warning(
                        "Повторная отправка не удалась для %s",
                        user.telegram_id,
                        exc_info=True,
                    )
                    continue
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
            else:
                logger.warning(
                    "Не удалось отправить уведомление пользователю %s",
                user.telegram_id,
                exc_info=True,
            )
    logger.info("Уведомления отправлены %d из %d пользователей", sent, len(users))


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
