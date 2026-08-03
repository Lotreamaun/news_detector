"""
Telegram-обработчики команд бота.
"""

import logging

from sqlalchemy import select
from telegram import Update
from telegram.ext import ContextTypes

from app.models import User
from app.services.gigachat import (
    GigaChatAPIError,
    GigaChatAuthError,
    GigaChatClient,
    GigaChatConfig,
)

logger = logging.getLogger(__name__)


def _session_maker(context: ContextTypes.DEFAULT_TYPE):
    """Достает фабрику сессий, положенную в bot_data при старте приложения."""
    return context.bot_data["session_maker"]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /start: регистрирует пользователя в БД и приветствует.

    Пользователь считается зарегистрированным один раз — при повторном
    /start запись не дублируется (проверка по уникальному telegram_id).
    """
    if update.effective_user is None or update.message is None:
        return
    telegram_id = update.effective_user.id
    username = update.effective_user.username

    async with _session_maker(context)() as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user is None:
            session.add(User(telegram_id=telegram_id, username=username))
            await session.commit()
            logger.info("Зарегистрирован новый пользователь telegram_id=%s", telegram_id)
        else:
            logger.info("Пользователь telegram_id=%s уже зарегистрирован", telegram_id)

    await update.message.reply_text(
        "Привет! Я буду присылать саммари новых законов с pravo.gov.ru.\n"
        "Сейчас сервис в разработке. Доступные команды:\n"
        "/start — регистрация\n"
        "/test — проверка связи с GigaChat API"
    )


async def test_gigachat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обработчик /test: smoke-тест подключения к GigaChat API.

    Получает access-токен, список моделей и отправляет короткий запрос.
    При ошибке не падает, а отвечает пользователю понятным текстом.
    """
    if update.message is None:
        return
    config = context.bot_data["config"]

    client = GigaChatClient(
        GigaChatConfig(
            auth_key=config.GIGACHAT_AUTH_KEY,
            model=config.GIGACHAT_MODEL,
            verify_ssl=config.GIGACHAT_VERIFY_SSL,
        )
    )

    try:
        async with client:
            token = await client.get_access_token()
            models = await client.get_models()
            reply = await client.complete(
                [{"role": "user", "content": "Ответь одним словом: привет"}]
            )
    except GigaChatAuthError as exc:
        await update.message.reply_text(f"Ошибка авторизации GigaChat:\n{exc}")
        return
    except GigaChatAPIError as exc:
        await update.message.reply_text(f"Ошибка GigaChat API:\n{exc}")
        return
    except Exception as exc:
        # Любая другая ошибка — не молчим, показываем пользователю
        logger.exception("Неожиданная ошибка в /test")
        await update.message.reply_text(f"Неожиданная ошибка:\n{exc}")
        return

    await update.message.reply_text(
        "GigaChat работает.\n"
        f"Токен получен: {token[:12]}...\n"
        f"Доступно моделей: {len(models)}\n"
        f"Тестовый ответ модели: {reply[:200]}"
    )
