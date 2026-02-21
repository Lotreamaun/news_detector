"""
Конфигурация сервиса News Detector
"""

import os  # Импорт стандартного модуля os. Дает доступ к переменным окружения через os.getenv
from dataclasses import dataclass
    # Декоратор @dataclass генерирует шаблонный код в классе, 
    # который предназначен преимущественно для хранения данных

    # Пример:

    #  без декоратора
    #     class User:
    #         def __init__(self, id: int, name: str, active: bool = True):
    #             self.id = id
    #             self.name = name
    #             self.active = active

    #         def __repr__(self) -> str:
    #             return f"User(id={self.id!r}, name={self.name!r}, active={self.active!r})"

    #         def __eq__(self, other):
    #             if not isinstance(other, User):
    #                 return NotImplemented
    #             return (
    #                 self.id == other.id
    #                 and self.name == other.name
    #                 and self.active == other.active
    #             )

    #  с декоратором
    #     @dataclass
    #     class User:
    #         id: int
    #         name: str
    #         active: bool = True

    # Args:

    #     frozen — изменяемость (=True делает объект неизменяемым)
from dotenv import load_dotenv
    # Функция load_dotenv() читает .env и добавляет переменные окружения в os.environ
    # Потом доступ ко всем переменным окружения можно получить через 

    # os.getenv('TELEGRAM_BOT_TOKEN')
    # или
    # os.environ.get('TELEGRAM_BOT_TOKEN')


class ConfigError(RuntimeError):  # Наследуется от встроенного класса RuntimeError
    """Появляется, когда переменные окружения в .env отсутствуют или задан не корректный тип данных."""

# Подчеркивание перед названием - маркер "приватной функции", которую не надо вызывать за пределами модуля
def _get_int_env(name: str, default: int) -> int:
    """
    Вычисляет значение переменных окружения при создании экземпляра класса

    Args:
        name: название переменной
        default: значение по умолчанию, если переменной нет

    Returns:
        Целое число (int)
        
    Raises:
        Поднимает ConfigError, если строку невозможно преобразовать в число
    """
    # Берет переменную окружения name или использует default, но приводит к строке 
    # strip() обрезает пробелы по краям значений. Напр., если в окружении написано "CHECK_INTERVAL_MINUTES= 15"
    raw = os.getenv(name, str(default)).strip()
    
    try:
        return int(raw)  # Теперь преобразуем строку в целое число (int)
    # ValueError - исключение, если тип данных правильный, но значение не подходит 
    # (напр. попытка преобразовать "abc" в int)
    except ValueError as e:
        raise ConfigError(f"Invalid integer for {name}: {raw!r}") from e

@dataclass(frozen=True, slots=True)
class Config:
    TELEGRAM_BOT_TOKEN: str
    DATABASE_URL: str

    # Optional settings (KISS defaults, aligned with README).
    CHECK_INTERVAL_MINUTES: int
    SUMMARY_MIN_LEN: int
    SUMMARY_MAX_LEN: int
    LOG_LEVEL: str

    @classmethod
    def load(cls) -> "Config":
        # Do not override existing system environment variables.
        load_dotenv(override=False)

        telegram_bot_token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
        database_url = (os.getenv("DATABASE_URL") or "").strip()

        missing: list[str] = []
        if not telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not database_url:
            missing.append("DATABASE_URL")
        if missing:
            raise ConfigError(
                "Missing required environment variable(s): "
                + ", ".join(missing)
                + ". Create a .env file or set them in the environment."
            )

        check_interval_minutes = _get_int_env("CHECK_INTERVAL_MINUTES", 15)
        summary_min_len = _get_int_env("SUMMARY_MIN_LEN", 140)
        summary_max_len = _get_int_env("SUMMARY_MAX_LEN", 280)
        log_level = os.getenv("LOG_LEVEL", "INFO").strip() or "INFO"

        if check_interval_minutes <= 0:
            raise ConfigError("CHECK_INTERVAL_MINUTES must be > 0")
        if summary_min_len <= 0:
            raise ConfigError("SUMMARY_MIN_LEN must be > 0")
        if summary_max_len <= 0:
            raise ConfigError("SUMMARY_MAX_LEN must be > 0")
        if summary_min_len > summary_max_len:
            raise ConfigError("SUMMARY_MIN_LEN must be <= SUMMARY_MAX_LEN")

        return cls(
            TELEGRAM_BOT_TOKEN=telegram_bot_token,
            DATABASE_URL=database_url,
            CHECK_INTERVAL_MINUTES=check_interval_minutes,
            SUMMARY_MIN_LEN=summary_min_len,
            SUMMARY_MAX_LEN=summary_max_len,
            LOG_LEVEL=log_level,
        )