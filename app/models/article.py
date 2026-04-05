"""Модель закона/документа с портала (идемпотентность по external_id)."""

from datetime import datetime

from sqlalchemy import DateTime, String, Text  # в string фиксированная длина, в text - произвольная
from sqlalchemy.orm import (
    Mapped, # обертка для типов
    mapped_column # настраивает колонку
)

from app.models.base import Base


class Article(Base):
    """
    Закон или документ: внешний ID, текст, саммари и ссылка на оригинал.

    Уникальный ``external_id`` гарантирует, что один документ с pravo.gov.ru
    обрабатывается и сохраняется не более одного раза.
    """

    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)  # последний параметр нужен, чтобы id расставлялись сами
    external_id: Mapped[str] = mapped_column(String(512), unique=True, index=True)  # ограниченная длина, уникальность, алф. указатель
    title: Mapped[str] = mapped_column(String(1024))
    original_text: Mapped[str | None] = mapped_column(Text(), nullable=True)
    summary: Mapped[str | None] = mapped_column(Text(), nullable=True)
    url: Mapped[str] = mapped_column(String(2048))  # принятое в практике макс. длина для url
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),  # нужно сохранять дату вместе с часовым поясом
        nullable=True,
    )
