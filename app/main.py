"""
Точка входа приложения: инициализация БД и запуск Telegram-бота.
"""

import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler

from telegram import BotCommand, Update
from telegram.ext import Application, CommandHandler

from app.bot.handlers import help_command, latest, start, test_gigachat
from app.core.config import Config
from app.core.database import (
    close_database,
    get_session_maker,
    init_database,
    init_db_schema,
)
from app.services.scheduler import check_legislation_updates

logger = logging.getLogger(__name__)


def _configure_logging(config: Config) -> None:
    """Консоль + файл (app.log) с суточной ротацией и хранением 30 дней."""
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(config.LOG_LEVEL)

    console = logging.StreamHandler()
    console.setLevel(config.LOG_LEVEL)
    console.setFormatter(formatter)
    root.addHandler(console)

    if config.LOG_FILE:
        file_handler = TimedRotatingFileHandler(
            config.LOG_FILE,
            when="midnight",  # ротация каждый день в полночь
            backupCount=config.LOG_RETENTION_DAYS,
            encoding="utf-8",
        )
        file_handler.setLevel(config.LOG_LEVEL)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)


def _build_application(config: Config) -> Application:
    """Собирает PTB-приложение: хранилище, обработчики, хуки жизненного цикла."""
    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_shutdown(_on_shutdown)
        .post_init(_post_init)
    )
    if config.TELEGRAM_PROXY_URL:
        # Для РФ api.telegram.org часто блокируется, поэтому прокси
        # настраивается опционально через TELEGRAM_PROXY_URL в .env
        builder = builder.proxy(config.TELEGRAM_PROXY_URL)

    application = builder.build()

    # Достаем из config -> bot_data, чтобы хендлеры не создавали Config сами
    application.bot_data["session_maker"] = get_session_maker()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("test", test_gigachat))
    application.add_handler(CommandHandler("latest", latest))
    application.add_handler(CommandHandler("help", help_command))

    return application


async def _on_shutdown(application: Application) -> None:
    """Закрывает пул соединений БД при остановке (graceful shutdown)."""
    await close_database()
    logger.info("Соединения с БД закрыты")


async def _init_database_schema(config: Config) -> None:
    """Разово создает таблицы БД (в отдельном event loop до старта бота)."""
    init_database(config.DATABASE_URL)
    await init_db_schema()


def _schedule_jobs(application: Application, config: Config) -> None:
    """Регистрирует периодическую проверку RSS в JobQueue PTB."""
    job_queue = application.job_queue
    if job_queue is None:
        logger.warning("JobQueue недоступен: периодическая проверка RSS не запущена")
        return

    interval_seconds = config.CHECK_INTERVAL_MINUTES * 60
    # first не задаём: в PTB/APScheduler run_repeating(first=0) НЕ запускает
    # job мгновенно (документированная ловушка). Первый прогон сразу после
    # старта делает _run_initial_check, дальше — по интервалу.
    job_queue.run_repeating(
        check_legislation_updates,
        interval=interval_seconds,
        name="check_legislation_updates",
    )
    logger.info("Проверка RSS запланирована: каждые %d минут", config.CHECK_INTERVAL_MINUTES)


async def _post_init(application: Application) -> None:
    """post_init: регистрирует список команд и запускает первый прогон проверки."""
    await _register_commands(application)
    await _run_initial_check(application)


async def _register_commands(application: Application) -> None:
    """Регистрирует список команд бота (видно при наборе '/' в чате)."""
    commands = [
        BotCommand("start", "Регистрация пользователя"),
        BotCommand("latest", "Последние обработанные законы"),
        BotCommand("test", "Проверка связи с GigaChat API"),
        BotCommand("help", "Справка по командам"),
    ]
    try:
        await application.bot.set_my_commands(commands)
        logger.info("Список команд зарегистрирован")
    except Exception:
        logger.exception("Не удалось зарегистрировать список команд")


async def _run_initial_check(application: Application) -> None:
    """Прогоняет проверку публикаций сразу после старта бота (post_init).

    PTB/APScheduler не умеют запускать run_repeating мгновенно (first=0 не
    работает — документированная ловушка), поэтому первый прогон делаем
    вручную через job.run. Идемпотентность по external_id защищает от дублей.
    """
    job_queue = application.job_queue
    if job_queue is None:
        return
    job = job_queue.get_jobs_by_name("check_legislation_updates")
    if not job:
        logger.warning("Job проверки публикаций не найден — первый прогон пропущен")
        return
    logger.info("Первый прогон проверки публикаций сразу после старта")
    await job[0].run(application)


def main() -> None:
    """Запускает бота: конфиг -> БД -> polling -> graceful shutdown."""
    config = Config.load()
    _configure_logging(config)
    logger.info("Конфигурация загружена, подключаемся к БД...")

    asyncio.run(_init_database_schema(config))
    logger.info("Таблицы БД готовы")

    application = _build_application(config)
    _schedule_jobs(application, config)
    # run_polling - блокирующий метод: сам создаёт event loop и обрабатывает
    # Ctrl+C. Поэтому его нельзя вызывать внутри asyncio.run.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
