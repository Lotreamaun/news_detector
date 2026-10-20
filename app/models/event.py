"""Модель журнала событий (действия пользователей, доставка, конвейер)."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Event(Base):
    """
    Одно событие журнала: что (``kind``), когда (``at``, UTC), от кого и уточнение.

    ``telegram_id`` — не внешний ключ на ``users``: у трекера активности он известен до
    ``/start``, а исключение администраторов (``ADMIN_CHAT_IDS`` — Telegram ID) остаётся
    простым ``NOT IN``. У системных событий (проверки, подборка) он NULL.
    ``ref`` — уточнение: команда, источник/исход, вид уведомления, число и т. п.
    """

    __tablename__ = "events"
    __table_args__ = (Index("ix_events_kind_at", "kind", "at"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)
    ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
