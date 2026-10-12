"""
Принудительная саммаризация закона по external_id.

Общее ядро для двух точек входа — бота (``/summary``, кнопки «Сделать саммари»)
и WebApp (``POST /summarize``). Сервис не знает про Telegram и не формирует
тексты сообщений: возвращает ``ForceSummaryResult`` со статусом, а каждая точка
входа сама выбирает формулировки.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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
from app.services.summary_usage import current_month

logger = logging.getLogger(__name__)

# уровни, для которых месячный лимит принудительной саммаризации не действует
AUTO_LEVELS: frozenset[str] = IMPORTANT_LEVELS


@dataclass(frozen=True)
class ForceSummaryResult:
    """Итог принудительной саммаризации.

    status: ``ok`` — саммари получено; ``limit`` — исчерпан месячный лимит;
    ``no_text`` — не удалось получить текст закона; ``refused`` — модель отказалась
    или вернула пустой ответ; ``error`` — прочий сбой.
    text заполнен всегда, когда текст закона был получен (в том числе при
    ``refused`` и ``error``), иначе None. title_known=False — у записи
    псевдозаголовок (title == external_id). from_cache=True — модель не вызывалась,
    саммари взято из БД.
    """

    status: Literal["ok", "limit", "no_text", "refused", "error"]
    title: str
    url: str
    title_known: bool
    summary: str | None = None
    text: str | None = None
    from_cache: bool = False


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




# Выполняющиеся саммаризации: external_id -> задача. Запись живёт, пока задача идёт.
_inflight: dict[str, asyncio.Task[ForceSummaryResult]] = {}


def inflight_task(external_id: str) -> asyncio.Task[ForceSummaryResult] | None:
    """Выполняющаяся принудительная саммаризация документа (None — не выполняется).

    Для фона планировщика: он только наблюдает за реестром и сам задач в него не кладёт.
    """
    return _inflight.get(external_id)


def _on_task_done(external_id: str, task: asyncio.Task[ForceSummaryResult]) -> None:
    """Снимает задачу с реестра; исключение задачи (не отмену) пишет в лог."""
    if _inflight.get(external_id) is task:
        del _inflight[external_id]
    if not task.cancelled() and task.exception() is not None:
        logger.error(
            "Саммаризация %s завершилась исключением", external_id, exc_info=task.exception()
        )


async def force_summarize(
    config, session_maker, user: User, external_id: str
) -> ForceSummaryResult:
    """Выполняет принудительную саммаризацию закона и возвращает результат без доставки.

    Порядок: присоединение к выполняющейся саммаризации документа (без лимита и списания) →
    лечение псевдозаголовка → готовое саммари из БД (без LLM и без лимита) →
    месячный лимит (обходят админы и ``AUTO_LEVELS``) → общая задача по документу
    (``_summarize_document``: текст / OCR → саммари → сохранение) → +1 к счётчику инициатору.
    По документу выполняется не более одной задачи. Сбои модели и непредвиденные ошибки
    возвращаются статусами ``refused`` / ``error``.
    """
    task = _inflight.get(external_id)
    if task is not None:
        logger.info(
            "Саммари %s уже в работе — присоединяем запрос user=%s", external_id, user.telegram_id
        )
        return await asyncio.shield(task)

    is_admin = user.telegram_id in getattr(config, "ADMIN_CHAT_IDS", ())
    month = current_month()
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
            from_cache=True,
        )

    # авто-уровни без лимита (Конституция/ФКЗ/ФЗ) — по уже сохранённой (и вылеченной) статье
    auto_bypass = cached is not None and cached.level in AUTO_LEVELS
    charge = not is_admin and not auto_bypass

    if charge:
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

    # Повторная проверка реестра: пока шли await выше, задачу мог создать другой запрос.
    # Между проверкой и записью в реестр await нет — две задачи на документ не возникнут.
    task = _inflight.get(external_id)
    initiator = task is None
    if task is None:
        logger.info("Саммари %s: запуск (user=%s)", external_id, user.telegram_id)
        task = asyncio.create_task(_summarize_document(config, session_maker, external_id))
        _inflight[external_id] = task
        task.add_done_callback(lambda t: _on_task_done(external_id, t))
    else:
        logger.info(
            "Саммари %s уже в работе — присоединяем запрос user=%s", external_id, user.telegram_id
        )

    # shield: отмена этого запроса не отменяет работу, которую ждут остальные
    result = await asyncio.shield(task)

    if initiator and charge and result.status == "ok" and not result.from_cache:
        async with session_maker() as session:
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
    return result


async def _summarize_document(
    config, session_maker, external_id: str
) -> ForceSummaryResult:
    """Документная часть саммаризации: не знает про пользователя, итог отдаётся всем ожидающим.

    Начинает с перечитывания ``Article``: саммари уже есть — ``ok`` с ``from_cache=True``
    без портала и модели. Дальше текст / OCR → сохранение текста → саммари → сохранение
    или создание ``Article``. Лимит не проверяет и не списывает. Исключения наружу не выходят.
    """
    async with session_maker() as session:
        cached = await session.scalar(
            select(Article).where(Article.external_id == external_id)
        )

    title = (cached.title if cached else None) or external_id
    url = (
        (cached.url if cached else None)
        or f"http://publication.pravo.gov.ru/document/{external_id}"
    )
    cached_known = _has_real_title(cached, external_id)

    if cached is not None and cached.summary:
        logger.info("Саммари %s уже в БД — модель не вызываем", external_id)
        return ForceSummaryResult(
            status="ok",
            title=title,
            url=url,
            title_known=cached_known,
            summary=cached.summary,
            text=cached.original_text or None,
            from_cache=True,
        )

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
                    priority="user",
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
                priority="user",
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
