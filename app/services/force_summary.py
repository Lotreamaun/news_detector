"""
Принудительная саммаризация закона по external_id.

Общее ядро для двух точек входа — бота (``/summary``, кнопки «Сделать саммари»)
и WebApp (``POST /summarize``). Сервис не знает про Telegram и не формирует
тексты сообщений: возвращает ``ForceSummaryResult`` со статусом, а каждая точка
входа сама выбирает формулировки.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from sqlalchemy import select

from app.models import Article, SummarizationUsage, User
from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError
from app.services.rss_parser import (
    IMPORTANT_LEVELS,
    FeedEntry,
    classify_level_for_title,
    fetch_document,
    get_legal_text,
    ocr_document_text,
)
from app.services.summarizer import Summarizer, SummarizerConfig

logger = logging.getLogger(__name__)

# уровни, для которых месячный лимит принудительной саммаризации не действует
AUTO_LEVELS: frozenset[str] = IMPORTANT_LEVELS


@dataclass
class ForceSummaryResult:
    """Итог принудительной саммаризации.

    status: ``ok`` — саммари получено; ``limit`` — исчерпан месячный лимит;
    ``no_text`` — не удалось получить текст закона; ``refused`` — модель отказалась
    или вернула пустой ответ; ``error`` — прочий сбой.
    text заполнен всегда, когда текст закона был получен (в том числе при
    ``refused`` и ``error``), иначе None. title_known=False — у записи
    псевдозаголовок (title == external_id).
    """

    status: Literal["ok", "limit", "no_text", "refused", "error"]
    title: str
    url: str
    title_known: bool
    summary: str | None = None
    text: str | None = None


def _has_real_title(article: Article | None, external_id: str) -> bool:
    """True, если у записи настоящее название, а не псевдозаголовок (пусто или == external_id)."""
    return article is not None and bool(article.title) and article.title != external_id


def _apply_document_metadata(
    article: Article, entry: FeedEntry | None, external_id: str
) -> None:
    """Проставляет записи название, ссылку и уровень из метаданных портала.

    entry=None (портал не ответил / не знает документ) — оставляем псевдозаголовок
    external_id и канонический url; уровень не трогаем.
    """
    if entry is None:
        article.title = article.title or external_id
        article.url = article.url or f"http://publication.pravo.gov.ru/document/{external_id}"
        return
    article.title = entry.title
    article.url = entry.url
    article.level = classify_level_for_title(entry.title, entry.document_type_id)


async def force_summarize(
    config, session_maker, user: User, external_id: str
) -> ForceSummaryResult:
    """Выполняет принудительную саммаризацию закона и возвращает результат без доставки.

    Порядок: лечение псевдозаголовка → готовое саммари из БД (без LLM и без лимита) →
    месячный лимит (обходят админы и ``AUTO_LEVELS``) → текст / OCR через GigaChat →
    сохранение текста → саммари → сохранение/создание ``Article`` → +1 к счётчику.
    Сбои модели и непредвиденные ошибки возвращаются статусами ``refused`` / ``error``.
    """
    is_admin = user.telegram_id in getattr(config, "ADMIN_CHAT_IDS", ())
    month = datetime.now().strftime("%Y-%m")
    limit = config.FORCE_SUMMARIZE_MONTHLY_LIMIT

    # title/url берём из сохранённой записи — они нужны во всех исходах ниже
    async with session_maker() as session:
        cached = await session.scalar(
            select(Article).where(Article.external_id == external_id)
        )

    # Лечение записи с псевдозаголовком (закон не из ленты, сохранённый раньше
    # без метаданных): один раз пробуем получить настоящие название/ссылку/уровень.
    if cached is not None and not _has_real_title(cached, external_id):
        entry = await fetch_document(external_id)
        if entry is not None:
            async with session_maker() as session:
                art = await session.scalar(
                    select(Article).where(Article.external_id == external_id)
                )
                if art is not None:
                    _apply_document_metadata(art, entry, external_id)
                    await session.commit()
                    cached = art

    title = (cached.title if cached else None) or external_id
    url = (
        (cached.url if cached else None)
        or f"http://publication.pravo.gov.ru/document/{external_id}"
    )
    cached_known = _has_real_title(cached, external_id)

    # Готовое саммари отдаём до проверки лимита: оно ничего не стоит
    if cached is not None and cached.summary:
        return ForceSummaryResult(
            status="ok",
            title=cached.title or external_id,
            url=cached.url,
            title_known=cached_known,
            summary=cached.summary,
            text=cached.original_text or None,
        )

    # авто-уровни без лимита (Конституция/ФКЗ/ФЗ) — по уже сохранённой (и вылеченной) статье
    auto_bypass = cached is not None and cached.level in AUTO_LEVELS

    if not is_admin and not auto_bypass:
        async with session_maker() as session:
            usage = await session.scalar(
                select(SummarizationUsage).where(
                    SummarizationUsage.user_id == user.id,
                    SummarizationUsage.month == month,
                )
            )
            if usage is not None and usage.count >= limit:
                return ForceSummaryResult(
                    status="limit", title=title, url=url, title_known=cached_known
                )

    # TODO: Рассмотреть платный тариф — бесконечная принудительная саммаризация

    text: str | None = None
    title_known = cached_known
    try:
        text = await get_legal_text(external_id)
        if not text:
            logger.info(
                "Текст для %s отсутствует — используем OCR через GigaChat",
                external_id,
            )
            async with GigaChatClient(
                GigaChatConfig(
                    auth_key=config.GIGACHAT_AUTH_KEY,
                    verify_ssl=config.GIGACHAT_VERIFY_SSL,
                )
            ) as ocr_client:
                text = await ocr_document_text(external_id, client=ocr_client)

        if not text:
            return ForceSummaryResult(
                status="no_text", title=title, url=url, title_known=cached_known
            )

        # Сохраняем распознанный текст сразу (до саммаризации), чтобы он был
        # доступен в WebApp независимо от исхода саммари — даже при отказе LLM.
        async with session_maker() as session:
            art = await session.scalar(
                select(Article).where(Article.external_id == external_id)
            )
            if art is not None:
                if not art.original_text:
                    art.original_text = text
                    await session.commit()

        async with Summarizer(
            SummarizerConfig(
                auth_key=config.GIGACHAT_AUTH_KEY,
                model=config.GIGACHAT_MODEL,
                min_len=config.SUMMARY_MIN_LEN,
                max_len=config.SUMMARY_MAX_LEN,
                verify_ssl=config.GIGACHAT_VERIFY_SSL,
            )
        ) as summarizer:
            summary = await summarizer.summarize(text)

        async with session_maker() as session:
            article = await session.scalar(
                select(Article).where(Article.external_id == external_id)
            )
            if article is not None:
                article.original_text = text
                article.summary = summary
                await session.commit()
                await session.refresh(article)
            else:
                # Закон не из дневной ленты (не отслеживался ботом) — создаём запись,
                # чтобы повторный запрос для этого же id переиспользовал саммари, а не
                # бил в LLM заново. notified=True — иначе ближайший цикл шедулера разошлёт
                # её всем подписчикам как будто это новая публикация (см. Article.notified).
                # Название/ссылку/уровень берём с портала; не ответил — псевдозаголовок.
                article = Article(
                    external_id=external_id,
                    original_text=text,
                    summary=summary,
                    notified=True,
                )
                _apply_document_metadata(article, await fetch_document(external_id), external_id)
                session.add(article)
                await session.commit()
            title_known = _has_real_title(article, external_id)
            title = article.title
            url = article.url

            if not is_admin and not auto_bypass:
                usage = await session.scalar(
                    select(SummarizationUsage).where(
                        SummarizationUsage.user_id == user.id,
                        SummarizationUsage.month == month,
                    )
                )
                if usage is None:
                    usage = SummarizationUsage(user_id=user.id, month=month, count=0)
                    session.add(usage)
                usage.count += 1
                await session.commit()
            else:
                await session.commit()

        return ForceSummaryResult(
            status="ok",
            title=title,
            url=url,
            title_known=title_known,
            summary=summary,
            text=text,
        )
    except GigaChatError as exc:
        logger.warning("Саммаризация %s: языковая модель %s", external_id, exc)
        async with session_maker() as session:
            art = await session.scalar(
                select(Article).where(Article.external_id == external_id)
            )
            if art is not None:
                if not art.original_text:
                    art.original_text = text
                    await session.commit()
            else:
                # Тот же случай, что и в успешном пути: закон не из ленты, но текст
                # реально получен — сохраняем, чтобы не терять его и не показывать
                # external_id как будто это заголовок.
                art = Article(
                    external_id=external_id,
                    original_text=text,
                    notified=True,
                )
                _apply_document_metadata(art, await fetch_document(external_id), external_id)
                session.add(art)
                await session.commit()
            title_known = _has_real_title(art, external_id)
            title = art.title
            url = art.url
        refused = "отказался" in str(exc) or "пустой ответ" in str(exc)
        return ForceSummaryResult(
            status="refused" if refused else "error",
            title=title,
            url=url,
            title_known=title_known,
            text=text,
        )
    except Exception:
        logger.exception("Ошибка принудительной саммаризации %s", external_id)
        return ForceSummaryResult(
            status="error", title=title, url=url, title_known=cached_known, text=text
        )
