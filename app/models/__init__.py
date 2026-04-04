"""ORM-модели: пользователи и законы."""

from app.models.article import Article
from app.models.base import Base
from app.models.user import User

__all__ = ["Base", "Article", "User"]
