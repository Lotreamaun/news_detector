"""
Создание краткого саммари документа через GigaChat API.

Fallback-поведение (vision.md): при любой ошибке GigaChat исключение
``GigaChatError`` пробрасывается наверх, а вызывающий код (scheduler)
сохраняет документ без саммари и уведомляет пользователя ссылкой на оригинал.

Промпты вынесены в константы модуля (prompt management, см. vision.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.services.gigachat import GigaChatClient, GigaChatConfig, GigaChatError, classify_reply

logger = logging.getLogger(__name__)

# Промпты хранятся отдельно, а не внутри функции
_SYSTEM_PROMPT = (
    "Ты — юридический ассистент. Составь краткую выжимку нормативного "
    "правового акта: что он устанавливает, для кого предназначен и какие "
    "ключевые положения содержит. Отвечай только текстом выжимки."
)
_USER_PROMPT_TEMPLATE = (
    "Закон (документ):\n\n{text}\n\n"
    "Сделай выжимку объёмом от {min_len} до {max_len} символов."
)

# Ограничиваем входной текст, чтобы не упираться в контекст модели
_MAX_INPUT_CHARS = 8_000

# Ответ модели может быть длиннее лимита — такой объём токенов про запас
_MAX_OUTPUT_TOKENS = 400


@dataclass(frozen=True, slots=True)
class SummarizerConfig:
    """Настройки саммаризатора (берутся из Config в .env)."""

    auth_key: str  # ключ GigaChat вида "Basic <секрет>"
    model: str
    min_len: int  # минимальная длина выжимки в символах
    max_len: int  # максимальная длина выжимки в символах
    verify_ssl: bool = False


class Summarizer:
    """
    Обёртка над GigaChatClient с промптом под краткую выжимку.

    Поддерживает ``async with Summarizer(config) as s:`` для автоматического
    управления сессией клиента GigaChat.
    """

    def __init__(self, config: SummarizerConfig) -> None:
        self._config = config
        self._client = GigaChatClient(
            GigaChatConfig(
                auth_key=config.auth_key,
                model=config.model,
                verify_ssl=config.verify_ssl,
            )
        )

    async def __aenter__(self) -> "Summarizer":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Закрывает сессию клиента GigaChat."""
        await self._client.close()

    async def summarize(self, text: str) -> str:
        """
        Возвращает выжимку документа заданной длины.

        Args:
            text: полный текст документа.

        Returns:
            Саммари (не пустая строка, обрезана до ``max_len``).

        Raises:
            GigaChatError: пустой вход, ошибка авторизации или API GigaChat.
        """
        if not text.strip():
            raise GigaChatError("Пустой текст документа — нечего саммаризировать")

        if len(text) > _MAX_INPUT_CHARS:
            logger.warning(
                "Текст документа урезан с %d до %d символов",
                len(text), _MAX_INPUT_CHARS,
            )
            text = text[:_MAX_INPUT_CHARS]

        user_prompt = _USER_PROMPT_TEMPLATE.format(
            text=text,
            min_len=self._config.min_len,
            max_len=self._config.max_len,
        )

        logger.info("Запрашиваем саммари (вход %d символов)...", len(text))
        reply = await self._client.complete(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )

        summary = reply.strip()
        _kind = classify_reply(summary)
        if _kind == "refusal":
            raise GigaChatError("GigaChat отказался делать саммари (чувствительная тема)")
        if _kind == "empty":
            raise GigaChatError("GigaChat вернул пустой ответ — саммари не получено")
        self._check_length(summary)

        if len(summary) > self._config.max_len:
            logger.warning(
                "Саммари длиннее лимита (%d > %d) — обрезаем",
                len(summary), self._config.max_len,
            )
            summary = summary[: self._config.max_len].rstrip() + "…"
        return summary

    def _check_length(self, summary: str) -> None:
        """Логирует, уложилась ли выжимка в требуемый диапазон длин."""
        length = len(summary)
        if length < self._config.min_len:
            logger.warning("Саммари короче минимума: %d < %d", length, self._config.min_len)
        else:
            logger.info("Саммари получено: %d символов", length)
