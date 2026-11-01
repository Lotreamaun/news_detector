"""Модель оценки недельной подборки пользователем."""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, ForeignKey, SmallInteger, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WeeklyFeedback(Base):
    """
    Оценка подборки периода: один голос на человека, учитывается последний.

    ``verdict`` — ``up`` / ``down``; ``reason`` — индекс причины 0–3 (только при ``down``, иначе NULL).
    """

    __tablename__ = "weekly_feedback"

    run_id: Mapped[int] = mapped_column(ForeignKey("weekly_digest_runs.id"), primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger(), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(8))
    reason: Mapped[int | None] = mapped_column(SmallInteger(), nullable=True)
    answered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
