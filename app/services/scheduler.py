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

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telegram.ext import ContextTypes

from app.models import Article, User
from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError
from app.services.rss_parser import (
    RssError,
    fetch_documents,
    get_legal_text,
    is_important,
    ocr_document_text,
)
from app.services.summarizer import Summarizer, SummarizerConfig

logger = logging.getLogger(__name__)


async def check_legislation_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк: проверяет API публикаций, обрабатывает новые законы."""
    config = context.bot_data["config"]
    session_maker = context.bot_data["session_maker"]

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
    article = Article(
        external_id=entry.external_id,
        title=entry.title,
        original_text=text,
        summary=summary,
        url=entry.url,
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
    """Рассылает уведомление о документе всем активным пользователям."""
    async with session_maker() as session:
        users = (
            await session.scalars(select(User).where(User.is_active.is_(True)))
        ).all()

    if not users:
        logger.debug("Активных пользователей для рассылки нет")
        return

    message = _build_notification(article)
    sent = 0
    for user in users:
        try:
            await context.bot.send_message(chat_id=user.telegram_id, text=message)
            sent += 1
        except Exception:
            logger.warning(
                "Не удалось отправить уведомление пользователю %s",
                user.telegram_id,
                exc_info=True,
            )
    logger.info("Уведомления отправлены %d из %d пользователей", sent, len(users))


def _build_notification(article: Article) -> str:
    """Формирует текст уведомления; без саммари — fallback со ссылкой."""
    if article.summary:
        return f"{article.title}\n\n{article.summary}\n\n{article.url}"
    # Fallback (vision.md): GigaChat недоступен или нет текста — ссылка на оригинал
    return f"{article.title}\n\nНе удалось проанализировать этот закон. Оригинал: {article.url}"
