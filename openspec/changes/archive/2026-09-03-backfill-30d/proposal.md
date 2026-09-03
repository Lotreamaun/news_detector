## Why

`fetch_documents` берёт только 1 страницу `PeriodType=daily` (30 последних с сегодня), поэтому законы за последние 30 дней (например 309-ФЗ от 04.08.2026) не попадают в БД, `/latest` показывает региональный мусор вместо ФЗ. Пагинация `currentPage`/`page`/`offset` в API публикаций игнорируется — всегда отдаёт первую страницу. Исследование показало рабочий путь: `periodType=day&date=<dd.mm.yyyy>` возвращает документы **конкретного дня** (total 546 за 04.08), максимальный размер страницы `pageSize=200`. Нужен одноразовый бэкфилл 30 дней через этот эндпоинт, дальше — только daily без бэкфилла.

## What Changes

- **Одноразовый бэкфилл** при первом запуске (`is_first_run` + пустая БД): для каждого из последних 30 дней один запрос `GET /api/documents?periodType=day&date=<dd.mm.yyyy>&pageSize=200&documentTypes=FZ&documentTypes=FKZ` (серверный фильтр только ФЗ/ФКЗ), сохранение в `Article` (без LLM, без summary).
- **Ежедневная инкрементальная синхронизация** (как сейчас): `PeriodType=daily` → новые документы → сохранить в `Article`.
- `/latest` — чистый `SELECT` из БД по `UserFilter` на календарное окно 30 дней, мгновенно. Смена фильтра не запускает пересинхронизацию — данные уже в БД.

## Capabilities

### New Capabilities
- `sync-30d`: одноразовый бэкфилл 30 дней через `periodType=day` (Date → документы дня)

### Modified Capabilities
- `law-force-filter`: бэкфилл 30 дней при первом запуске (по дням через periodType=day)
- `bot-handlers/latest`: теперь реально есть ФЗ/ФКЗ за 30 дней для показа

## Impact

- `app/services/rss_parser.py`: новая функция `fetch_day(day, document_type_ids=...)` — `GET /api/documents?periodType=day&date=...&pageSize=200&documentTypes=...`, очистка сломанной пагинации `currentPage` из `fetch_documents`
- `app/services/scheduler.py`: `check_legislation_updates` / `_run_initial_check` — флаг `is_first_run` + ветка бэкфилла (`_run_backfill`, 31 день, серверный фильтр ФЗ/ФКЗ) без `_notify_users`
- `app/bot/handlers.py`: `/latest` и `show_example` — календарное окно 30 дней через `_window_start` (иначе отсекается закон утром 30 дней назад), показывают ФЗ/ФКЗ после бэкфилла
- Без LLM, без новых зависимостей.
