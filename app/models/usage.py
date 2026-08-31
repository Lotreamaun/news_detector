"""Модель учёта принудительных саммаризаций (лимит на пользователя в месяц)."""

from typing import Optional

from sqlalchemy import (
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class SummarizationUsage(Base):
    """
    Счётчик принудительных саммаризаций пользователя за календарный месяц.

    Ключ — ``(user_id, month)`` (month вида ``YYYY-MM``). Уникальное
    ограничение не даёт дублировать счётчик одного пользователя за один
    месяц; новая строка создаётся при первом использовании в месяце.
    """

    __tablename__ = "summarization_usage"
    __table_args__ = (
        UniqueConstraint("user_id", "month", name="uq_usage_user_month"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), index=True, nullable=False
    )
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    count: Mapped[int] = mapped_column(Integer(), default=0, nullable=False)
