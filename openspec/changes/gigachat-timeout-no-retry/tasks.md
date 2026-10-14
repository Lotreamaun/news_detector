## 1. Таймаут и повторы в `app/services/gigachat.py`

- [x] 1.1 Класс `GigaChatTimeoutError(GigaChatAPIError)` с docstring. `_request_json_once` на `asyncio.TimeoutError` бросает его с `retryable=True`, в сообщении указан фактический таймаут (design D1)
- [x] 1.2 `_request_json_once` и `_request_json` принимают `timeout: float | None = None`. Если значение задано, в `session.request(...)` передаётся `aiohttp.ClientTimeout(total=timeout)`; без него таймаут сессии остаётся (design D3)
- [x] 1.3 `_request_json` принимает `retry_timeout: bool = True`. При `False` ошибка `GigaChatTimeoutError` пробрасывается сразу, без строки «Повтор запроса»; повторы на 429/5xx/сетевые ошибки не меняются (design D2)
- [x] 1.4 Константа `_TIMEOUT_COOLDOWN_SECONDS = 60` с комментарием, откуда значение (лог 08.10, design D4)
- [x] 1.5 `_api_request` принимает `generation: bool = False` и `timeout: float | None = None`, передаёт `retry_timeout=not generation` и `timeout` в `_request_json`. При `generation` внутри `async with _stream_slot(...)` ловит `GigaChatTimeoutError`: `logger.warning` (URL, таймаут в секундах, пауза потока), `await asyncio.sleep(_TIMEOUT_COOLDOWN_SECONDS)`, затем пробрасывает ошибку (design D2)
- [x] 1.6 `complete(..., timeout: float | None = None)` вызывает `_api_request(..., generation=True, timeout=timeout)`. В docstring описать новый аргумент и что генерация после таймаута не повторяется, а поток держится паузой. `upload_file`, `delete_file`, `get_models` и OAuth не меняются
- [x] 1.7 Обновить комментарий у `_RETRYABLE_STATUS` и docstring `GigaChatConfig.timeout` (таймаут по умолчанию, генерацию можно переопределить на вызов)

## 2. Таймаут OCR в `app/services/rss_parser.py`

- [x] 2.1 Константа `_OCR_TIMEOUT_SECONDS = 180.0` рядом с `_MAX_OCR_TOKENS`. `ocr_document_text` передаёт её в `client.complete(..., timeout=_OCR_TIMEOUT_SECONDS)` (design D3)
- [x] 2.2 Проверить grep-ом, что остальные вызовы `complete` (`summarizer.py`, `importance.py`, `scripts/`) не требуют правок и остались с таймаутом 60 с

## 3. Проверка

- [x] 3.1 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.main` без ошибок
- [x] 3.2 Скрипт в scratchpad (не в репозитории), без сети и токена (`get_access_token` — заглушка, `_TIMEOUT_COOLDOWN_SECONDS` подменён на ~0.5 с, `_request_json_once` — заглушка с журналом вызовов). Проверить: (а) `complete` с таймаутом → ровно одна попытка, `GigaChatTimeoutError` у вызывающего, в логе предупреждение о таймауте и паузе, строки «Повтор запроса» нет; (б) пока идёт пауза, ожидающий запрос не стартует, а стартует после неё; ошибка возвращается вызывающему только после паузы; (в) во время паузы приходят фоновый, затем пользовательский запрос → после паузы первым идёт пользовательский; (г) `delete_file` с таймаутом → повторяется, паузы нет; (д) `complete` с 429, 429, ok → повторы как раньше, паузы нет; (е) отмена задачи во время паузы → следующий ожидающий стартует сразу; (ж) `complete(timeout=180)` → в `session.request` уходит `ClientTimeout(total=180)`, `complete()` без таймаута → аргумента нет (заглушка сессии вместо `_request_json_once`)
- [x] 3.3 Ручная проверка на локальном боте с **отдельным** ключом GigaChat, совмещённая с задачей 4.3 change `gigachat-single-stream-queue`. Во время цикла планировщика с OCR нажать «Сделать саммари» в Mini-App по документу без саммари. Ожидается: OCR длиной 60–180 с завершается успешно; если таймаут всё же случился, в логе есть строка паузы и нет каскада `HTTP 429` после неё; пользовательский вызов идёт раньше следующих фоновых _(15.10: прод остановлен, ключ один. Подтверждено: OCR 89 с проходит при таймауте 180 с; при таймауте 5 с строка паузы, удаление файла через 60 с, без 429 и «Повтор запроса». Приоритет пользователя над фоном живьём проверяется в 4.3 change `gigachat-single-stream-queue`.)_

## 4. Документация и порядок архивации

- [x] 4.1 `CLAUDE.md`: в строке `app/services/gigachat.py` таблицы «Key modules» дописать, что генерация после таймаута не повторяется и поток держится паузой 60 с; в описании OCR упомянуть таймаут 180 с
- [x] 4.2 `docs/PLAN.md`: отметить п. 5 выполненным. В P2 п. 11 (`gigachat-orphan-files-cleanup`) отметить, что удаление файла после таймаута OCR теперь идёт после паузы; дубли от повтора `upload_file` остаются
- [ ] 4.3 После 3.3: через `/opsx:update gigachat-single-stream-queue` уточнить требование «Повторы без потери очереди» (таймаут генерации не повторяется, см. `gigachat-request-timeouts`), закрыть его задачу 4.3. Архивировать сначала `gigachat-single-stream-queue`, затем этот change
