"""
Разовая подгрузка федеральных актов за текущее окно недельной подборки.

Нужна, чтобы проверить ``/test_digest weekly`` на реальных данных, пока бот ещё не
накопил свежих актов с ``created_at``. Что делает:

1. Берёт с pravo.gov.ru федеральные ФКЗ, ФЗ, указы Президента и постановления
   Правительства, опубликованные в окне (после пятницы ``WEEKLY_DIGEST_TIME``).
2. Недостающие обрабатывает так же, как бот при приёме (текст → саммари → сохранение →
   оценка важности), но с ``notified=True``: пользователям ничего не уходит.
3. Уже имеющиеся в БД акты окна без ``created_at`` (принятые до миграции) получают
   ``created_at`` по дате публикации, исправленный уровень, текст, саммари и оценку.

Акты без текста остаются неоценёнными: их доберёт обычный добор бота (OCR через сутки).

Запуск из корня репозитория (расходует квоту GigaChat)::

    python -m scripts.seed_weekly_window --dry-run   # только посчитать
    python -m scripts.seed_weekly_window
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import select

import app.services.scheduler as scheduler
from app.core.config import Config
from app.core.database import get_session_maker, init_database, init_db_schema
from app.models import Article
from app.services.importance import CANDIDATE_LEVELS
from app.services.rss_parser import (
    _FKZ_DOCUMENT_TYPE_ID,
    _FZ_DOCUMENT_TYPE_ID,
    classify_level_for_title,
    fetch_day,
    get_legal_text,
)
from app.services.weekly_window import last_digest_moment

# Указы и постановления: GUID видов актов в справочнике publication.pravo.gov.ru
_DECREE_TYPE_ID = "0790e34b-784b-4372-884e-3282622a24bd"
_RESOLUTION_TYPE_ID = "fd5a8766-f6fd-4ac2-8fd9-66f414d314ac"
_TYPE_IDS = [_FZ_DOCUMENT_TYPE_ID, _FKZ_DOCUMENT_TYPE_ID, _DECREE_TYPE_ID, _RESOLUTION_TYPE_ID]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, ничего не менять")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("app.services.gigachat").setLevel(logging.WARNING)
    logging.getLogger("app.services.summarizer").setLevel(logging.WARNING)

    config = Config.load()
    init_database(config.DATABASE_URL)
    await init_db_schema()
    session_maker = get_session_maker()

    tz = ZoneInfo(config.WEEKLY_DIGEST_TZ)
    now = datetime.now(timezone.utc)
    start = last_digest_moment(now, config.WEEKLY_DIGEST_TIME or scheduler.WEEKLY_DEFAULT_TIME, tz)
    # Акты, опубликованные в день рассылки до её времени, относятся к прошлому окну
    first_day = start.date() + timedelta(days=1)
    print(f"Окно: {start:%d.%m %H:%M} — сейчас; публикации с {first_day:%d.%m.%Y}")

    entries = []
    day = first_day
    while day <= datetime.now(tz).date():
        for entry in await fetch_day(day.strftime("%d.%m.%Y"), config.PRAVO_API_URL, document_type_ids=_TYPE_IDS):
            if classify_level_for_title(entry.title, entry.document_type_id) in CANDIDATE_LEVELS:
                entries.append(entry)
        day += timedelta(days=1)

    async with session_maker() as session:
        existing = {
            a.external_id: a
            for a in (
                await session.scalars(select(Article).where(Article.external_id.in_([e.external_id for e in entries])))
            ).all()
        }
    new_entries = [e for e in entries if e.external_id not in existing]
    old_rows = [a for a in existing.values() if a.created_at is None and not a.is_demo]
    print(f"Федеральных актов в окне: {len(entries)}; новых: {len(new_entries)}; "
          f"в БД без created_at: {len(old_rows)}")
    if args.dry_run:
        return

    context = SimpleNamespace(bot_data={"config": config, "session_maker": session_maker})

    # Новые акты — как при приёме, но без рассылки (notified=True)
    original_save = scheduler._save_article

    async def save_silently(session_maker, entry, text, summary, *, notified=False):
        return await original_save(session_maker, entry, text, summary, notified=True)

    scheduler._save_article = save_silently
    try:
        async with scheduler._llm_tools(config) as (summarizer, llm_client):
            for entry in new_entries:
                await scheduler._process_entry(context, session_maker, summarizer, llm_client, entry)

            # Старые строки окна: created_at, уровень, текст, саммари, оценка
            by_id = {e.external_id: e for e in entries}
            for article in old_rows:
                entry = by_id[article.external_id]
                values = {
                    "created_at": entry.published_at or now,
                    "level": classify_level_for_title(article.title, entry.document_type_id),
                }
                text = article.original_text or await get_legal_text(article.external_id)
                summary = article.summary
                if text and not summary:
                    try:
                        summary = await summarizer.summarize(text)
                    except Exception as exc:  # отказ/сбой: оценка пойдёт по началу текста
                        print(f"  саммари {article.external_id} не получено: {exc}")
                values.update(original_text=text, summary=summary)
                await scheduler._update_article(session_maker, article.id, **values)
                async with session_maker() as session:
                    article = await session.get(Article, article.id)
                await scheduler._rate_article(
                    context, session_maker, summarizer, llm_client, article, retry_text=False, summary_tried=True
                )
    finally:
        scheduler._save_article = original_save

    text, _, report = await scheduler.build_weekly_preview(session_maker, config)
    print("\n" + report)


if __name__ == "__main__":
    asyncio.run(main())
