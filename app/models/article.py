"""Модель закона/документа с портала (идемпотентность по external_id)."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Article(Base):
    """
    Закон или документ: внешний ID, текст, саммари и ссылка на оригинал.

    Уникальный ``external_id`` гарантирует, что один документ с pravo.gov.ru
    обрабатывается и сохраняется не более одного раза.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    external_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(1024))
    original_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    url: Mapped[str] = mapped_column(String(2048))
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
