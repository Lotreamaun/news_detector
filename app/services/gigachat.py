"""
Асинхронный клиент GigaChat API (aiohttp).

Получение и кэширование access-токена (с автообновлением по истечении),
отправка chat-completions запросов, список доступных моделей.

Пример::

    config = GigaChatConfig(auth_key="Basic <секретный_код>")
    async with GigaChatClient(config) as client:
        reply = await client.complete([{"role": "user", "content": "Привет!"}])
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import time
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)

# Запас времени до истечения токена (сек): не даем использовать токен "на грани"
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Ретраится только: сетевые ошибки, таймауты, 429, 5xx.
# Остальные HTTP-ошибки (400, 401, 403 и т.д.) падают сразу.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GigaChatError(RuntimeError):
    """Базовая ошибка GigaChat API."""


class GigaChatAuthError(GigaChatError):
    """Нет ключа, не удалось получить токен или ключ невалидный."""


class GigaChatAPIError(GigaChatError):
    """HTTP-ошибка или невалидный ответ GigaChat API.

    Attributes:
        status: HTTP-статус ответа (None для сетевых ошибок).
        retryable: можно ли безопасно повторить запрос позже.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class GigaChatConfig:
    """
    Настройки клиента GigaChat.

    Attributes:
        auth_key: ключ авторизации вида ``Basic <секретный_код>``
            (задается через ``GIGACHAT_AUTH_KEY`` в ``.env``).
        scope: область доступа OAuth (по умолчанию персональный API).
        model: модель для chat-completions по умолчанию.
        oauth_url: эндпоинт получения access-токена.
        api_base_url: базовый URL API (без завершающего слеша).
        timeout: таймаут HTTP-запроса в секундах.
        max_retries: сколько раз повторить временную ошибку.
        verify_ssl: проверять ли TLS-сертификат.
    """

    auth_key: str
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat-2-Max"
    oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    api_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    timeout: float = 60.0
    max_retries: int = 3
    verify_ssl: bool = False


class GigaChatClient:
    """
    Клиент к GigaChat: кэширует токен, делает запросы с ретраями.

    Поддерживает ``async with GigaChatClient(config) as client:`` для
    автоматического управления сессией aiohttp.
    """

    def __init__(self, config: GigaChatConfig) -> None:
        self._config = config
        self._session: aiohttp.ClientSession | None = None
        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._token_lock = asyncio.Lock()

    # Управление жизненным циклом

    async def __aenter__(self) -> "GigaChatClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()

    async def close(self) -> None:
        """Закрывает сессию aiohttp, если она открыта."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    # Публичное API

    async def get_access_token(self) -> str:
        """
        Возвращает валидный access-токен, получая новый по необходимости.

        Токен кэшируется в клиенте. Параллельные вызовы не создают
        несколько запросов к OAuth (защита через asyncio.Lock).
        """
        async with self._token_lock:
            if self._token_is_fresh():
                return self._access_token or ""
            self._access_token = await self._request_access_token()
            return self._access_token

    async def complete(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        attachments: list[str] | None = None,
        function_call: str | None = None,
    ) -> str:
        """
        Отправляет диалог в chat-completions и возвращает текст ответа.

        Args:
            messages: список сообщений вида ``{"role": ..., "content": ...}``.
            model: имя модели (по умолчанию берется из конфигурации).
            temperature: «креативность» ответа (0.0–1.0).
            max_tokens: максимальное число токенов в ответе.
            attachments: id файлов из хранилища GigaChat (POST /files).
                Передаются массивом строк внутри последнего user-сообщения
                (формат из доки «Работа с файлами»); чтобы модель учла файлы,
                для текстовых документов сервер сам вызывает встроенную
                функцию ``get_file_content``.
            function_call: режим функций (``"auto"``, ``"none"`` или имя).
                При attachments по умолчанию выставляется ``"auto"`` —
                включается автоматический режим чтения приложенных файлов.

        Returns:
            Текст ответа модели.

        Raises:
            GigaChatAuthError: не задан ключ или не удалось получить токен.
            GigaChatAPIError: ошибка API или пустой ответ.
        """
        token = await self.get_access_token()

        payload: dict[str, Any] = {
            "model": model or self._config.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if attachments:
            payload["function_call"] = function_call or "auto"
            if messages:
                last = dict(messages[-1])
                last["attachments"] = list(attachments)
                payload["messages"] = [*messages[:-1], last]

        data = await self._request_json(
            "POST",
            f"{self._config.api_base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            json=payload,
        )
        return self._extract_reply(data)

    async def upload_file(
        self,
        data: bytes,
        *,
        filename: str,
        mime: str,
        purpose: str = "general",
    ) -> str:
        """
        Загружает файл в хранилище GigaChat и возвращает его id.

        Id можно передавать в ``attachments`` запроса ``complete``.
        Использовать файл может только тот пользователь, кто его загрузил
        (по умолчанию — Client ID проекта).

        Args:
            data: содержимое файла.
            filename: имя файла (например, ``0001202608040001.pdf``).
            mime: MIME-тип (например, ``application/pdf``).
            purpose: назначение файла; ``"general"`` — для генераций.

        Returns:
            Идентификатор загруженного файла.
        """
        token = await self.get_access_token()

        form = aiohttp.FormData()
        form.add_field("purpose", purpose)
        form.add_field("file", data, filename=filename, content_type=mime)

        body = await self._request_json(
            "POST",
            f"{self._config.api_base_url}/files",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            data=form,
        )
        file_id = body.get("id")
        if not file_id:
            raise GigaChatAPIError(f"Ответ POST /files не содержит id: {body}")
        logger.info("Файл %s загружен в хранилище GigaChat (file=%s)", filename, file_id)
        return str(file_id)

    async def delete_file(self, file_id: str) -> None:
        """
        Удаляет файл из хранилища GigaChat (гигиена после OCR).

        Args:
            file_id: идентификатор файла, полученный при загрузке.

        Raises:
            GigaChatAuthError: не задан ключ или не удалось получить токен.
            GigaChatAPIError: ошибка API.
        """
        token = await self.get_access_token()
        await self._request_json(
            "POST",
            f"{self._config.api_base_url}/files/{file_id}/delete",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
            expect_json=False,
        )
        logger.info("Файл %s удалён из хранилища GigaChat", file_id)

    async def get_models(self) -> list[str]:
        """Возвращает список доступных моделей (для отладки и TODO-проверок)."""
        token = await self.get_access_token()
        data = await self._request_json(
            "GET",
            f"{self._config.api_base_url}/models",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
            },
        )
        return [item.get("id", "") for item in data.get("data", [])]

    # -- Внутреннее ----------------------------------------------------

    def _token_is_fresh(self) -> bool:
        if not self._access_token:
            return False
        return self._token_expires_at > time.time() + _TOKEN_REFRESH_MARGIN_SECONDS

    def _make_ssl_context(self) -> ssl.SSLContext:
        """
        Создает SSL-контекст для aiohttp.

        GigaChat использует собственный корневой сертификат, поэтому по
        умолчанию проверка отключена.
        TODO(security): скачать корневой CA из документации GigaChat,
        добавить в доверенные и переключить ``GIGACHAT_VERIFY_SSL=true``,
        затем этот блок можно удалить.
        """
        context = ssl.create_default_context()
        if self._config.verify_ssl:
            return context
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    async def _request_access_token(self) -> str:
        auth_key = self._config.auth_key
        # developers.sber.ru отдаёт ключ без префикса "Basic " — добавляем сами
        if auth_key and not auth_key.startswith("Basic "):
            auth_key = f"Basic {auth_key}"
            logger.info("К GIGACHAT_AUTH_KEY автоматически добавлен префикс 'Basic '")

        if not auth_key:
            raise GigaChatAuthError(
                "GIGACHAT_AUTH_KEY не задан. Скопируйте .env.example в .env "
                "и укажите ключ вида 'Basic <секретный_код>' "
                "(раздел 'Ключи авторизации' на developers.sber.ru)."
            )

        data = await self._request_json(
            "POST",
            self._config.oauth_url,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": auth_key,
            },
            data={"scope": self._config.scope},
        )

        token = data.get("access_token")
        if not token:
            raise GigaChatAuthError(f"Ответ OAuth не содержит access_token: {data}")

        # GigaChat возвращает expires_at (сек от эпохи). На случай
        # expires_in в секундах считаем срок сами.
        expires_at = data.get("expires_at")
        if isinstance(expires_at, (int, float)):
            self._token_expires_at = float(expires_at)
        elif isinstance(data.get("expires_in"), (int, float)):
            self._token_expires_at = time.time() + float(data["expires_in"])
        else:
            logger.warning("OAuth-ответ без срока жизни токена, кэшируем на 5 мин")
            self._token_expires_at = time.time() + 300

        logger.info("Получен новый access-токен GigaChat (срок до %.0f)", self._token_expires_at)
        return str(token)

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        """Выполняет запрос с ретраями на временные ошибки."""
        last_error: GigaChatError | None = None
        for attempt in range(self._config.max_retries):
            try:
                return await self._request_json_once(
                    method,
                    url,
                    headers=headers,
                    data=data,
                    json=json,
                    expect_json=expect_json,
                )
            except GigaChatError as exc:
                last_error = exc
                if not exc.retryable or attempt == self._config.max_retries - 1:
                    raise
                logger.warning(
                    "Повтор запроса %s %s (попытка %d/%d): %s",
                    method, url, attempt + 1, self._config.max_retries, exc,
                )
                await asyncio.sleep(2**attempt)
        raise last_error  # недостижимо, но типо-безопасно для статики

    async def _request_json_once(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        expect_json: bool = True,
    ) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                data=data,
                json=json,
            ) as resp:
                try:
                    body = await resp.json(content_type=None)
                except ValueError as exc:
                    # Некоторые эндпоинты (напр. удаление файла) возвращают
                    # пустое тело с 200 — для них expect_json=False.
                    if not expect_json and resp.status < 400:
                        return {}
                    # Не-JSON ответ (бывает при 400/401 на OAuth) — ловим,
                    # чтобы не уронить хендлер молча
                    text = await resp.text()
                    raise GigaChatAPIError(
                        f"Ответ от {url} не является JSON (HTTP {resp.status}): {text[:300]}",
                        status=resp.status,
                        retryable=resp.status in _RETRYABLE_STATUS,
                    ) from exc
                if resp.status >= 400:
                    retryable = resp.status in _RETRYABLE_STATUS
                    raise GigaChatAPIError(
                        f"GigaChat API вернул HTTP {resp.status} для {url}: {body}",
                        status=resp.status,
                        retryable=retryable,
                    )
                return body
        except aiohttp.ClientError as exc:
            raise GigaChatAPIError(
                f"Сетевая ошибка при запросе {url}: {exc}", retryable=True
            ) from exc
        except asyncio.TimeoutError as exc:
            raise GigaChatAPIError(
                f"Таймаут запроса {url} ({self._config.timeout}s)", retryable=True
            ) from exc
        except GigaChatAPIError:
            raise
        except Exception as exc:  # страхуюсь от неожиданных ошибок стека
            logger.exception("Неожиданная ошибка при запросе %s %s", method, url)
            raise GigaChatAPIError(
                f"Неожиданная ошибка при запросе {url}: {exc}", retryable=True
            ) from exc

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self._config.timeout),
                connector=aiohttp.TCPConnector(ssl=self._make_ssl_context()),
            )
        return self._session

    @staticmethod
    def _extract_reply(data: dict[str, Any]) -> str:
        """Достает текст ответа из структуры chat-completions."""
        try:
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise GigaChatAPIError(f"Не удалось извлечь ответ из данных: {data}") from exc
        if not content:
            raise GigaChatAPIError("GigaChat вернул пустой ответ (возможно, цензура)")
        return content
