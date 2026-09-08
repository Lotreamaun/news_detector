"""Проверка подписки пользователя на обязательный Telegram-канал."""

import logging

from telegram import Bot

logger = logging.getLogger(__name__)

_SUBSCRIBED_STATUSES = {"member", "administrator", "creator"}


async def is_subscribed(bot: Bot, telegram_id: int, channel_id: str | None) -> bool:
    """
    Проверяет, состоит ли пользователь в требуемом канале.

    Если ``channel_id`` не задан (гейт отключён), возвращает ``True`` без обращения
    к Telegram API. Любая ошибка запроса (бот не в канале, сетевой сбой и т.п.)
    трактуется как "не подписан" (fail-closed) и логируется на уровне WARNING.

    Args:
        bot: экземпляр Telegram-бота для вызова Bot API.
        telegram_id: id пользователя, чьё членство проверяется.
        channel_id: numeric chat id или ``@username`` требуемого канала.

    Returns:
        ``True``, если пользователь подписан (или гейт отключён), иначе ``False``.
    """
    if not channel_id:
        return True

    try:
        member = await bot.get_chat_member(chat_id=channel_id, user_id=telegram_id)
    except Exception:
        logger.warning(
            "Не удалось проверить подписку telegram_id=%s на канал %s "
            "(бот не добавлен в канал/сетевая ошибка) — считаем не подписанным",
            telegram_id,
            channel_id,
            exc_info=True,
        )
        return False

    return member.status in _SUBSCRIBED_STATUSES
