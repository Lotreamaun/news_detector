"""Модель пользователя Telegram для рассылки и идентификации."""

from datetime import datetime
# TODO: Выяснить как работает func и для чего нужен 
from sqlalchemy import (
    BigInteger,  # нужен для telegram_id, потому что это большие числа
    Boolean,  # 
    DateTime, 
    String, 
    func,  # с его помощью можно обращаться к функциям, которые вшиты в SQLAlchemy
    text  # позволяет пистаь SQL-запрос прямо в коде
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class User(Base):
    """
    Подписчик бота: идентификатор Telegram, имя и флаг активных уведомлений.

    Поле ``is_active`` позволяет отключить рассылку без удаления записи.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger(), unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # если пользователь заблокирует бота, здесь просто поменяется флаг на False
    is_active: Mapped[bool] = mapped_column(Boolean(),
    default=True,
    server_default=text("1")
    )
    # подтверждение подписки на обязательный Telegram-канал (гейт для новых регистраций,
    # server_default=True сохраняет текущее поведение для уже существующих строк)
    channel_verified: Mapped[bool] = mapped_column(
        Boolean(),
        default=True,
        server_default=text("1"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        # если при создании записи не будет указано created_at, 
        # установится значение по умолчанию — текущее время сервера БД
        server_default=func.now(),
    )
