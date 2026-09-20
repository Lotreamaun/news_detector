"""
Резервное копирование SQLite БД: консистентная копия (VACUUM INTO), ротация.

Запускается через PTB JobQueue (см. ``app/main.py::_schedule_jobs``), по
аналогии с ``check_legislation_updates``. Сбой бэкапа не должен ронять бота —
ошибка логируется и, если заданы ``ADMIN_CHAT_IDS``, уходит уведомление.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def resolve_sqlite_path(database_url: str) -> Path | None:
    """Возвращает путь к файлу SQLite из `DATABASE_URL`, либо None, если это не SQLite."""
    url = make_url(database_url)
    if not url.drivername.startswith("sqlite"):
        return None
    if not url.database:
        return None
    return Path(url.database)


async def create_backup(db_path: Path, backup_dir: Path) -> Path:
    """Создаёт консистентную копию `db_path` в `backup_dir` через `VACUUM INTO`.

    Выполняется синхронным `sqlite3` в отдельном потоке, чтобы не блокировать
    event loop бота на время копирования.
    """
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_path = backup_dir / f"{db_path.stem}_{timestamp}.db"

    def _do_backup() -> None:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("VACUUM INTO ?", (str(backup_path),))
        finally:
            conn.close()

    await asyncio.to_thread(_do_backup)
    return backup_path


def rotate_backups(backup_dir: Path, retention_count: int, db_path: Path) -> None:
    """Оставляет `retention_count` самых свежих файлов бэкапа в `backup_dir`, остальные удаляет.

    Файлы сортируются по имени — в имени закодирован timestamp
    (`<basename>_YYYYmmdd_HHMMSS.db`), так что сортировка по имени = по времени.
    `db_path` (сама, живая БД) исключается из кандидатов на удаление — защита от
    случая, когда `DB_BACKUP_DIR` по ошибке указан той же директорией, что и БД.
    """
    backups = sorted(p for p in backup_dir.glob("*.db") if p.resolve() != db_path.resolve())
    stale = backups[:-retention_count] if retention_count > 0 else backups
    for path in stale:
        try:
            path.unlink()
        except OSError:
            logger.exception("Не удалось удалить старый бэкап %s", path)


async def run_backup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue-колбэк: делает бэкап SQLite-файла, ротирует, уведомляет админов при сбое.

    Регистрируется в `JobQueue` только когда `DATABASE_URL` указывает на SQLite
    (см. `app/main.py::_schedule_jobs`), так что здесь путь гарантированно резолвится.
    """
    config = context.bot_data["config"]

    db_path = resolve_sqlite_path(config.DATABASE_URL)
    if db_path is None:
        return

    try:
        backup_dir = Path(config.DB_BACKUP_DIR)
        backup_path = await create_backup(db_path, backup_dir)
        rotate_backups(backup_dir, config.DB_BACKUP_RETENTION_COUNT, db_path)
        logger.info("Бэкап БД создан: %s", backup_path)
    except Exception as exc:
        logger.exception("Не удалось создать бэкап БД")
        await _notify_admins_of_failure(context, exc)


async def _notify_admins_of_failure(context: ContextTypes.DEFAULT_TYPE, exc: Exception) -> None:
    """Уведомляет всех `ADMIN_CHAT_IDS` о сбое бэкапа; не прерывает рассылку при блокировке ботом."""
    config = context.bot_data["config"]
    if not config.ADMIN_CHAT_IDS:
        return

    text = f"⚠️ Не удалось создать бэкап БД: {exc}"
    for chat_id in config.ADMIN_CHAT_IDS:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            logger.warning("Не удалось уведомить админа %s о сбое бэкапа", chat_id, exc_info=True)
