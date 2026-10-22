## 1. Лимит размера PDF (`app/services/rss_parser.py`)

- [ ] 1.1 Добавить `class TooLargeError(RssError)` рядом с `FetchError`/`ParseError` (не подкласс `FetchError`, чтобы `_fetch_with_retries` его не повторял — design.md, п. 2)
- [ ] 1.2 `_fetch_once`: необязательный `max_bytes: int | None = None`; при `as_bytes=True` и заданном лимите — `TooLargeError` по `resp.content_length > max_bytes` без чтения тела, иначе чтение кусками `resp.content.iter_chunked(64 * 1024)` в `bytearray` с `TooLargeError` при превышении (design.md, п. 1); без лимита поведение прежнее
- [ ] 1.3 `_fetch_with_retries`: пробросить `max_bytes` в `_fetch_once`, докстринг упомянуть, что `TooLargeError` не повторяется
- [ ] 1.4 `_download_pdf`: передать `max_bytes=_MAX_PDF_BYTES`, ловить `TooLargeError` → WARNING «PDF … слишком большой … — OCR пропускается» (с заявленным размером, если известен) и `None`; убрать проверку `len(data) > _MAX_PDF_BYTES` после загрузки; обновить докстринг

## 2. Старт и обработчик ошибок (`app/main.py`, `app/bot/handlers.py`)

- [ ] 2.1 `main()`: `application.run_polling(allowed_updates=Update.ALL_TYPES, bootstrap_retries=-1)` с коротким комментарием, зачем (таймаут Telegram на старте не должен ронять процесс)
- [ ] 2.2 `app/bot/handlers.py`: `async def error_handler(update, context)` с докстрингом — «message is not modified» → INFO одной строкой; `NetworkError`, кроме `BadRequest`, → WARNING одной строкой без трейсбека; прочее → ERROR с `exc_info=context.error` (design.md, п. 4); пользователю ничего не отправляет
- [ ] 2.3 `_build_application`: `application.add_error_handler(error_handler)`

## 3. Проверка и документация

- [ ] 3.1 Ручная проверка лимита скриптом в scratchpad: локальный aiohttp-сервер отдаёт (а) тело больше лимита с `Content-Length`, (б) то же chunked без `Content-Length`, (в) маленький файл; вызвать `_download_pdf(..., pdf_url=...)` с уменьшенным через monkeypatch `_MAX_PDF_BYTES`: (а) и (б) → `None`, одна строка WARNING, нет «Повтор запроса», в (а) тело не дочитывается; (в) → байты файла
- [ ] 3.2 Живая проверка на реальном большом PDF: `ocr_document_text("0001202610190010", client=...)` (или прямой `_download_pdf`) быстро возвращает `None` без роста памяти процесса, GigaChat не вызывается
- [ ] 3.3 Ручная проверка обработчика ошибок скриптом: вызвать `error_handler` с фейковым `context.error` = `TimedOut`, `BadRequest("Message is not modified: …")`, `BadRequest("Can't parse entities")`, `ValueError` — уровни и наличие трейсбека соответствуют спеке `secret-safe-logging`
- [ ] 3.4 (по возможности) Живая проверка старта: запустить бота с недоступным прокси/без сети к Telegram — процесс не завершается, в логе повторы; вернуть сеть — бот стартует; с неверным токеном — запуск останавливается
- [ ] 3.5 `python -c "from app.core.config import Config; print(Config.load())"` и импорт `app.main`, `app.bot.handlers`, `app.services.rss_parser` проходят
- [ ] 3.6 CLAUDE.md: в описании OCR (раздел «Processing one document») — PDF больше 40 МБ не скачивается и не распознаётся, акт идёт как «без текста»; в «Startup sequence» — `bootstrap_retries=-1` и обработчик ошибок PTB
