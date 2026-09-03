"""Фильтры пользователя по юридической силе закона."""

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class UserFilter(Base):
    """Строка фильтра: один выбранный уровень силы для одного пользователя.

    Комбинация (user_id, level) уникальна. Отсутствие строк = фильтр «Все».
    """

    __tablename__ = "user_filters"

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    level: Mapped[str] = mapped_column(String(32), primary_key=True)
