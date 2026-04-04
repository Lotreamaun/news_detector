"""Базовый класс декларативных моделей SQLAlchemy."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Базовый класс для всех ORM-моделей; общий metadata для create_all."""
