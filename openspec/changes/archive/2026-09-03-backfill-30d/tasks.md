## 1. Бэкфилл через periodType=day

- [x] 1.1 Обновить `LEVEL_MAP` для `FZ` на `82a8bf1c-3bc7-47ed-827f-7affd43a7f27` и `classify_level_for_title` fallback (done)
- [x] 1.2 В `rss_parser.py` добавить `fetch_day(day: date)` — `GET /api/documents?periodType=day&date={d:%d.%m.%Y}&pageSize=200` → `list[FeedEntry]`; удалить сломанную пагинацию `currentPage` из `fetch_documents` (вернуть одно-страничный daily)
- [x] 1.3 В `_run_initial_check` / `check_legislation_updates` добавить ветку бэкфилла: если `is_first_run` и пустая БД — `fetch_day` для каждого из 30 дней (без `_notify_users`), иначе daily без бэкфилла

## 2. Проверка

- [x] 2.1 `py_compile`, `openspec validate --changes`
- [x] 2.2 Ручная проверка: `/latest` за 30 дней (по `UserFilter`) показывает ФЗ/ФКЗ, `show_example` → 309-ФЗ
