"""Модели рассылок администратора: рассылка, доставка получателю и ответ на опрос."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Broadcast(Base):
    """
    Рассылка администратора выбранному сегменту пользователей.

    ``options`` — JSON-строка списка вариантов ответа (0–4). ``action_kind`` — кнопка действия:
    ``link`` (``action_label`` + ``action_url``), ``check_subscription`` или NULL.
    ``recipients`` — размер снимка сегмента в момент запуска. ``status``: ``sending`` → ``done``.
    ``client_token`` уникален: повтор той же формы не создаёт вторую рассылку.
    """

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(BigInteger())
    text: Mapped[str] = mapped_column(Text())
    options: Mapped[str] = mapped_column(Text(), default="[]")
    action_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    action_label: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action_url: Mapped[str | None] = mapped_column(Text(), nullable=True)
    segment: Mapped[str] = mapped_column(String(32))
    recipients: Mapped[int] = mapped_column(Integer())
    status: Mapped[str] = mapped_column(String(16), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    client_token: Mapped[str] = mapped_column(String(64), unique=True)


class BroadcastDelivery(Base):
    """
    Доставка рассылки одному получателю: снимок аудитории и состояние для возобновления.

    Строки создаются при запуске со статусом ``pending``; цикл отправки переводит каждую
    в ``sent`` / ``blocked`` (403) / ``error``.
    """

    __tablename__ = "broadcast_deliveries"

    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id"), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BroadcastAnswer(Base):
    """Ответ получателя на опрос рассылки: один на человека, учитывается последний (``option`` — индекс)."""

    __tablename__ = "broadcast_answers"

    broadcast_id: Mapped[int] = mapped_column(ForeignKey("broadcasts.id"), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    option: Mapped[int] = mapped_column(Integer())
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
