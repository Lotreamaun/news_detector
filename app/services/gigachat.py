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
import re
import ssl
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp

logger = logging.getLogger(__name__)

# Запас времени до истечения токена (сек): не даем использовать токен "на грани"
_TOKEN_REFRESH_MARGIN_SECONDS = 60

# Ретраится только: сетевые ошибки, таймауты, 429, 5xx.
# Остальные HTTP-ошибки (400, 401, 403 и т.д.) падают сразу.
# Исключение — таймаут генерации (chat/completions): не ретраится, см. ниже.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Пауза потока после таймаута генерации (сек). Клиент перестал ждать, а GigaChat
# доделывает генерацию и держит единственный поток ключа: по логу 08.10 таймаут
# на 60-й секунде, 429 держались ещё ~25 с (брошенная генерация шла ~85 с).
_TIMEOUT_COOLDOWN_SECONDS = 60

# -- Очередь запросов на процесс -----------------------------------------------
# У ключа GigaChat один поток, а клиентов в процессе много (планировщик,
# force-summarize, OCR), поэтому очередь модульная, а не на экземпляр клиента.
# Все запросы к API идут по одному; пользовательские — вперёд фоновых,
# внутри приоритета — FIFO. Выполняющийся запрос не прерывается.

Priority = Literal["user", "background"]

_stream_busy = False
_waiting: dict[Priority, deque[asyncio.Future[None]]] = {
    "user": deque(),
    "background": deque(),
}


def _release() -> None:
    """Передаёт поток первому неотменённому ожидающему (user, затем background)."""
    global _stream_busy
    for priority in ("user", "background"):
        queue = _waiting[priority]
        while queue:
            fut = queue.popleft()
            if not fut.done():
                fut.set_result(None)  # поток передан «из рук в руки», флаг остаётся
                return
    _stream_busy = False


def calls_ahead_of_user() -> int:
    """Сколько вызовов API окажется впереди нового пользовательского запроса.

    ``1`` при занятом потоке плюс число неотменённых ожидающих пользовательских.
    Только чтение.
    """
    waiting_users = sum(1 for fut in _waiting["user"] if not fut.done())
    return (1 if _stream_busy else 0) + waiting_users


@asynccontextmanager
async def _stream_slot(priority: Priority) -> AsyncIterator[None]:
    """Занимает единственный поток к API; выход из блока освобождает его."""
    global _stream_busy
    if not _stream_busy and not any(_waiting.values()):
        _stream_busy = True
    else:
        ahead = calls_ahead_of_user() if priority == "user" else (
            (1 if _stream_busy else 0) + sum(len(q) for q in _waiting.values())
        )
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        _waiting[priority].append(fut)
        started = time.monotonic()
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                _release()  # поток уже передан нам, но задачу отменили
            else:
                try:
                    _waiting[priority].remove(fut)
                except ValueError:
                    pass
            raise
        logger.info(
            "GigaChat: %s-запрос ждал очереди %.1f с (впереди было %d)",
            priority, time.monotonic() - started, ahead,
        )
    try:
        yield
    finally:
        _release()


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


class GigaChatTimeoutError(GigaChatAPIError):
    """Клиент не дождался ответа GigaChat за отведённое время.

    Сервер при этом может ещё какое-то время доделывать генерацию и держать
    поток ключа, поэтому для генерации такой запрос не повторяют сразу.
    """


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
        timeout: таймаут HTTP-запроса в секундах по умолчанию; генерации
            (``complete``) можно задать свой на вызов через ``timeout=``.
        max_retries: сколько раз повторить временную ошибку.
        verify_ssl: проверять ли TLS-сертификат.
        priority: приоритет в общей очереди запросов процесса: ``"user"`` —
            запрос живого пользователя (идёт вперёд фоновых), ``"background"`` —
            работа планировщика и скрипты. По умолчанию фон: забытое поле не даёт
            запросу обойти очередь.
    """

    auth_key: str
    scope: str = "GIGACHAT_API_PERS"
    model: str = "GigaChat-2-Max"
    oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    api_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    timeout: float = 60.0
    max_retries: int = 3
    verify_ssl: bool = False
    priority: Priority = "background"


class GigaChatClient:
    """
    Клиент к GigaChat: кэширует токен, делает запросы с ретраями.

    Все запросы к API процесса (со всех клиентов) идут по одному,
    пользовательские вперёд фоновых; OAuth-запрос токена вне очереди.

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
        timeout: float | None = None,
    ) -> str:
        """
        Отправляет диалог в chat-completions и возвращает текст ответа.

        После таймаута запрос не повторяется (GigaChat ещё доделывает брошенную
        генерацию): поток общей очереди держится паузой, затем ошибка уходит
        вызывающему. Повторы на 429, 5xx и сетевые ошибки работают как обычно.

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
            timeout: таймаут этого запроса в секундах (по умолчанию —
                ``config.timeout``); отсчёт с момента отправки в GigaChat.

        Returns:
            Текст ответа модели.

        Raises:
            GigaChatAuthError: не задан ключ или не удалось получить токен.
            GigaChatAPIError: ошибка API или пустой ответ
                (``GigaChatTimeoutError`` — таймаут).
        """
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

        data = await self._api_request(
            "POST",
            f"{self._config.api_base_url}/chat/completions",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            json=payload,
            generation=True,
            timeout=timeout,
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
        form = aiohttp.FormData()
        form.add_field("purpose", purpose)
        form.add_field("file", data, filename=filename, content_type=mime)

        body = await self._api_request(
            "POST",
            f"{self._config.api_base_url}/files",
            headers={"Accept": "application/json"},
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
        await self._api_request(
            "POST",
            f"{self._config.api_base_url}/files/{file_id}/delete",
            headers={"Accept": "application/json"},
            expect_json=False,
        )
        logger.info("Файл %s удалён из хранилища GigaChat", file_id)

    async def get_models(self) -> list[str]:
        """Возвращает список доступных моделей (для отладки и TODO-проверок)."""
        data = await self._api_request(
            "GET",
            f"{self._config.api_base_url}/models",
            headers={"Accept": "application/json"},
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

    async def _api_request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        data: Any | None = None,
        json: Any | None = None,
        expect_json: bool = True,
        generation: bool = False,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Запрос к API в общей очереди процесса (ретраи — внутри слота).

        Токен берётся уже внутри слота: ожидание очереди может занять минуты,
        и заранее полученный токен успел бы протухнуть.

        ``generation`` — запрос генерации: таймаут не ретраится, а слот ещё
        ``_TIMEOUT_COOLDOWN_SECONDS`` держится паузой, чтобы следующий запрос
        очереди не получил 429 от брошенной генерации.
        """
        async with _stream_slot(self._config.priority):
            token = await self.get_access_token()  # таймаут OAuth паузу не вызывает
            try:
                return await self._request_json(
                    method,
                    url,
                    headers={**headers, "Authorization": f"Bearer {token}"},
                    data=data,
                    json=json,
                    expect_json=expect_json,
                    retry_timeout=not generation,
                    timeout=timeout,
                )
            except GigaChatTimeoutError:
                if not generation:
                    raise
                logger.warning(
                    "Таймаут генерации %s (%s с): поток GigaChat на паузе %d с",
                    url, timeout or self._config.timeout, _TIMEOUT_COOLDOWN_SECONDS,
                )
                await asyncio.sleep(_TIMEOUT_COOLDOWN_SECONDS)
                raise

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        expect_json: bool = True,
        retry_timeout: bool = True,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Выполняет запрос с ретраями на временные ошибки.

        При ``retry_timeout=False`` таймаут пробрасывается сразу, без повтора.
        """
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
                    timeout=timeout,
                )
            except GigaChatError as exc:
                last_error = exc
                if isinstance(exc, GigaChatTimeoutError) and not retry_timeout:
                    raise
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
        timeout: float | None = None,
    ) -> dict[str, Any]:
        session = await self._get_session()
        # Без своего таймаута действует таймаут сессии (config.timeout)
        extra: dict[str, Any] = {}
        if timeout is not None:
            extra["timeout"] = aiohttp.ClientTimeout(total=timeout)
        applied_timeout = timeout if timeout is not None else self._config.timeout
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                data=data,
                json=json,
                **extra,
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
            raise GigaChatTimeoutError(
                f"Таймаут запроса {url} ({applied_timeout}s)", retryable=True
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


# — Детекция отказа LLM -----------------------------------------------------
# Ответ модели может быть отказом/дисклеймером (этический лимит, чувствительная
# тема, служебный буклет), а не содержанием саммари/OCR. Детекция — гибридная:
# точные маркеры-фразы + лёгкие regex-паттерны на синонимичные перефразировки.
# Точные маркеры дают низкие ложные срабатывания; паттерны расширяют полноту.

# «Сильные» фразы-буклеты: не встречаются в содержательном саммари закона.
# Именно по ним решаются длинные ответы (см. classify_reply, порог по длине).
_STRONG_REFUSAL_MARKERS = (
    "не обладают собственным мнением",
    "не обладает собственным мнением",
    "не имеют собственного мнения",
    "не имею собственного мнения",
    "не транслирует мнение",
    "не транслирую мнение",
    "не выражает собственное мнение",
    "ответ сгенерирован нейросетевой моделью",
    "ответ сгенерирован нейронной сетью",
    "сгенерирован нейросетевой моделью",
    "сгенерировано нейросетевой моделью",
    "обученной на открытых данных",
    "во избежание неправильного толк",
    "во избежание ошибочного толк",
    "разговоры на чувствительные тем",
    "чувствительные темы могут быть ограничены",
    "не могу выполнить это",
    "не могу предоставить саммари",
    "не могу составить саммари",
    "не могу помогать с этим",
    "это может нарушать",
    "нарушает законодательство",
)

# Общие маркеры отказа/ограничения (короткие ответы решаются по любому из них).
_GENERAL_REFUSAL_MARKERS = (
    "не могу",
    "не могу помочь",
    "не в состоянии",
    "не имею права",
    "отказаться",
    "отказываюсь",
    "не могу предоставить",
    "не могу выполнить",
    "не могу составить",
    "чувствительные тем",
    "нарушает",
    "этическ",
    "огранич",
    "недопустимо",
    "не могу сказать",
    "не могу дать",
    "не могу ответить",
    "извините",
    "cannot",
    "i can't",
    "i cannot",
    "unable to",
    "not able to",
    "won't help",
    "sorry",
)

# Компилируем паттерны-перефразировки один раз. Каждый требует связки
# «действие-отказ» с «темой ограничения» в одном окне, чтобы резать строки вроде
# «СССР ограничивал свободу» (закон/исторический текст), но ловить «не могу
# составить <...> из-за ограничений».
_REFUSAL_PATTERNS = (
    # (не) могу/в состоянии/имею права ... (отказать/ограничить/мнение/чувствит.)
    re.compile(
        r"(не\s+(?:могу|может|в\s+состоянии|имею\s+права|позволяю)).{0,60}"
        r"(?:отказать|огранич|чувствительн|мнени|этическ|предоставить|составить)",
        re.IGNORECASE,
    ),
    # связка «(не) является ... (отказом/ограничением/дисклеймером/мнением)»
    re.compile(
        r"(?:отказ|ограничен|дисклеймер|предостережен|мнение)[^\n]{0,40}"
        r"(?:модел|нейросет|сгенерир|саммари|текст)", re.IGNORECASE,
    ),
    # «не ... из-за (этических/политических/чувствительных) соображений»
    re.compile(
        r"не[^\n]{0,40}(?:этическ|чувствительн|политическ|безопасн)\s+соображен",
        re.IGNORECASE,
    ),
    # «(мне|я) не разрешено/нельзя ... (обсуждать/предоставить/составлять/...)»
    # — отказ на выполнение задачи (не «неразрешённые вопросы» в тексте закона).
    re.compile(
        r"(?:мне|я)\s+(?:не\s+разреш(?:ено|ается|ают)|нельзя|запрещ(?:ено|ается))"
        r"[^\n]{0,50}(?:обсужд|предостав|состав|отвеч|выполн|саммари|содержани)",
        re.IGNORECASE,
    ),
)

# Длина, выше которой ответ считается «длинным» и решается только по сильным
# маркерам (реальный саммари/OCR обычно длиннее и не содержит фраз-буклетов).
_LONG_REPLY_LIMIT = 600


def _has_strong_marker(lowered: str) -> bool:
    return any(m in lowered for m in _STRONG_REFUSAL_MARKERS)


def _has_general_marker(lowered: str) -> bool:
    return any(m in lowered for m in _GENERAL_REFUSAL_MARKERS)


def _matches_pattern(lowered: str) -> bool:
    return any(p.search(lowered) for p in _REFUSAL_PATTERNS)


def classify_reply(text: str | None) -> Literal["content", "refusal", "empty"]:
    """Классифицирует ответ GigaChat: рабочий контент, отказ или пусто.

    Возвращает ``"content"`` для содержательного саммари/OCR-текста,
    ``"empty"`` для пустого ответа и ``"refusal"`` для отказа/дисклеймера.
    Используется единой точкой входа всеми потребителями (саммари и OCR),
    чтобы не было рассинхронизации между путями генерации.

    Двухуровневый порог: короткие ответы решаются по любому маркеру/паттерну,
    длинные — только по «сильным» фразам-буклетам (D2 в design.md).
    """
    lowered = (text or "").strip().lower()
    if not lowered:
        return "empty"

    is_long = len(lowered) > _LONG_REPLY_LIMIT
    if is_long:
        if _has_strong_marker(lowered):
            return "refusal"
        return "content"

    if _has_strong_marker(lowered) or _has_general_marker(lowered):
        return "refusal"
    if _matches_pattern(lowered):
        return "refusal"
    return "content"


def looks_like_refusal(text: str) -> bool:
    """Тонкая обёртка над ``classify_reply`` (для совместимости с вызовами).

    Возвращает True, если ответ является отказом (но не пустым ответом).
    """
    return classify_reply(text) == "refusal"
