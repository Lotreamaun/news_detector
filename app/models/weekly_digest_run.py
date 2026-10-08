"""Модель учёта еженедельных рассылок (идемпотентность подборки по периоду)."""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class WeeklyDigestRun(Base):
    """
    Рассылка еженедельной подборки за один период.

    ``period_end`` — запланированный момент рассылки (пятница), уникален: один
    период — одна подборка. ``finished_at`` NULL до конца рассылки: при падении
    посреди неё следующий вызов отправит подборку заново (семантика «минимум раз»).
    """

    __tablename__ = "weekly_digest_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), unique=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
