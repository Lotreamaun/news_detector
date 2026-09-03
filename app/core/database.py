"""
Асинхронное подключение к SQLite (SQLAlchemy 2.0), сессии и создание таблиц.
"""

from typing import Optional  # аннотация типа: переманная может содержать тип или быть None

from sqlalchemy.ext.asyncio import (
    AsyncEngine,  # Класс-тип для движка: отвечает за связь с БД
    AsyncSession,  # Класс-тип для сессии (один сеанс работы)
    async_sessionmaker,  # Фабрика для создания сессий
    create_async_engine,  # Функция для создания объекта движка
)

from app.models import Base, Article, User, UserFilter  # SQLAlchemy "увидит" модели и создаст таблицы в metadata

_engine: Optional[AsyncEngine] = None  # приватная переменная для движка
_async_session_factory: Optional[async_sessionmaker[AsyncSession]] = None  # для сессий


# После оператора * идут строго именованные аргументы, т.е. его можно вызывать только так: "echo=True/False"
def init_database(database_url: str, *, echo: bool = False) -> None:
    """
    Создаёт асинхронный движок и фабрику сессий по URL из конфигурации.

    Ожидается URL вида ``sqlite+aiosqlite:///...`` (см. ``DATABASE_URL`` в ``.env``).
    Вызывается один раз при старте приложения.

    Args:
        database_url: Строка подключения SQLAlchemy.
        echo: при True будет печатать в консоль все SQL-запросы
    """
    global _engine, _async_session_factory  # global указывает, что хотим изменить первые две переменные, а не создавать локальные

    _engine = create_async_engine(database_url, echo=echo)  # асинхронный движок - один на все время работы бота
    # Фабрика сессий:
        # expire_on_commit=False: запрещает SQLAlchemy "обнулять" данные в объектах после сохранения в БД
        # autoflush=False: отключает автоматическую отправку изменений в БД после каждого изменения объекта
    _async_session_factory = async_sessionmaker(
        _engine,
        expire_on_commit=False,
        autoflush=False,
    )


async def close_database() -> None:
    """
    Закрывает пул соединений движка при остановке приложения (graceful shutdown).
    """
    global _engine, _async_session_factory

    if _engine is not None:
        await _engine.dispose()  # метод закрывает все активные соединения с файлом SQLite
    # обнуляем переменные _engine и _async_session_factory, чтобы освободить память
    _engine = None
    _async_session_factory = None


def get_engine() -> AsyncEngine:
    """
    Возвращает текущий асинхронный движок.

    Returns:
        Настроенный ``AsyncEngine``.

    Raises:
        RuntimeError: Если ``init_database`` ещё не вызывали.
    """
    if _engine is None:
        raise RuntimeError("Database is not initialized: call init_database() first.")
    return _engine


def get_session_maker() -> async_sessionmaker[AsyncSession]:
    """
    Возвращает фабрику асинхронных сессий.

    Пример::

        factory = get_session_maker()
        async with factory() as session:
            ...
            await session.commit()

    Returns:
        Настроенный ``async_sessionmaker``.

    Raises:
        RuntimeError: Если ``init_database`` ещё не вызывали.
    """
    if _async_session_factory is None:
        raise RuntimeError("Database is not initialized: call init_database() first.")
    return _async_session_factory


async def init_db_schema() -> None:
    """
    Создаёт все зарегистрированные таблицы, если их ещё нет (CREATE IF NOT EXISTS).

    Перед вызовом должны быть импортированы все модели (см. импорт ``User``, ``Article`` выше).
    """
    engine = get_engine()
    async with engine.begin() as connection:
        # run_sync запускает синхронную функцию create_all
        # Base.metadata.create_all: SQLite смотри все модели и создает таблицы, которых еще нет в БД
        await connection.run_sync(Base.metadata.create_all)
