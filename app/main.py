"""
Точка входа приложения: инициализация БД и запуск Telegram-бота.
"""

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler

from app.bot.handlers import start, test_gigachat
from app.core.config import Config
from app.core.database import (
    close_database,
    get_session_maker,
    init_database,
    init_db_schema,
)

logger = logging.getLogger(__name__)


def _configure_logging(config: Config) -> None:
    """Настраивает вывод логов в консоль (уровень из конфигурации)."""
    logging.basicConfig(
        level=config.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _build_application(config: Config) -> Application:
    """Собирает PTB-приложение: хранилище, обработчики, хуки жизненного цикла."""
    builder = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .post_shutdown(_on_shutdown)
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

    return application


async def _on_shutdown(application: Application) -> None:
    """Закрывает пул соединений БД при остановке (graceful shutdown)."""
    await close_database()
    logger.info("Соединения с БД закрыты")


async def _init_database_schema(config: Config) -> None:
    """Разово создает таблицы БД (в отдельном event loop до старта бота)."""
    init_database(config.DATABASE_URL)
    await init_db_schema()


def main() -> None:
    """Запускает бота: конфиг -> БД -> polling -> graceful shutdown."""
    config = Config.load()
    _configure_logging(config)
    logger.info("Конфигурация загружена, подключаемся к БД...")

    asyncio.run(_init_database_schema(config))
    logger.info("Таблицы БД готовы")

    application = _build_application(config)
    # run_polling - блокирующий метод: сам создаёт event loop и обрабатывает
    # Ctrl+C. Поэтому его нельзя вызывать внутри asyncio.run.
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
