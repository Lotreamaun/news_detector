"""Модель отметки о напоминании администраторам (идемпотентность по периоду)."""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WeeklyReviewPing(Base):
    """
    Служебное напоминание администраторам проверить недельную подборку.

    ``period_end`` — запланированный момент рассылки подборки (пятница), уникален:
    одно напоминание на период, в том числе после перезапуска бота.
    """

    __tablename__ = "weekly_review_pings"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    pinged_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
