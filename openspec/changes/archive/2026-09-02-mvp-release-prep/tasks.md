## 1. Удалить dev-команды

- [x] 1.1 Удалить `notify_me`, `myid_command`, `test_gigachat` из `app/bot/handlers.py` (функции + `HELP_TEXT`)
- [x] 1.2 Убрать регистрацию `CommandHandler("notify_me"/"myid"/"test")` и `BotCommand` из `app/main.py`
- [x] 1.3 `rg "notify_me|myid|test_gigachat"` по проекту — убедиться что нет остатков (кроме тестов/логов), проверить `OCR_TEST_EONUMBERS` уже удалён

## 2. Переделать /latest на подписки (без саммари)

- [x] 2.1 Переписать `app/bot/handlers.py:latest` — грузить `user` → `levels` (если пусто — дефолт `[Конституция,FKZ,FZ]` с подсказкой, а не «Все»), `spheres`/`region` (пусто = Все пока), строить `select(Article).where(level IN ...)` с `limit(10)`, рендер только `*title* + [Читать на портале](url)` с `MarkdownV2`, без `summary`
- [x] 2.2 Обновить сообщения пустого состояния: «Пока нет законов по вашим фильтрам. Настройте: /settings» vs «Пока нет обработанных законов»
- [x] 2.3 Обновить спек `specs/bot-handlers/latest/spec.md` (уже в change) — проверить рендер не ломает Markdown
- [x] 2.4 Расширить `app/services/rss_parser.py:LEVEL_MAP`/`is_important` на Конституцию/ФКЗ/ФЗ и bypass лимита `FORCE_SUMMARIZE_MONTHLY_LIMIT` для них в `handlers._summarize_and_reply` (авто-саммари без кнопки)
- [x] 2.5 Добавить окно 30 дней и заголовок: `published_at >= now-30d`, `ORDER BY published_at DESC`, заголовок `*Законы за последние 30 дней*` перед списком, пустое — «Пока нет законов за последние 30 дней по вашим фильтрам»

## 3. Убрать спам на старте + фолбэк/лимиты Telegram

- [x] 3.1 Изменить `app/main.py:_run_initial_check` / `app/services/scheduler.py:check_legislation_updates` — на первом прогоне после деплоя сохранять `Article` без `_notify_users` если `users` пусто или флаг `is_first_run` (только новые после старта рассылать)
- [x] 3.2 Проверить идемпотентность: повторный старт не досылает старые `external_id`
- [x] 3.3 Добавить в `scheduler._notify_users` обработку Telegram `429 RetryAfter` → `sleep`+`retry` и `403 Forbidden` → `is_active=False`, `asyncio.sleep(0.05)` между отправками; фолбэк при недоступности GigaChat/pravo — без саммари + ссылка (уже есть, проверить)

## 4. Актуализировать приветствие

- [x] 4.1 Обновить `app/bot/handlers.py:start` — новый текст ценности + CTA (`/latest` 10 по подпискам с дефолтом `[Конституция,ФКЗ,ФЗ]` + пример «например, юристу по крипте — оставь только ФЗ» в онбординге, `/settings`), убрать «в разработке»
- [x] 4.2 Обновить `app/bot/handlers.py:HELP_TEXT` — только прод-команды (`/start`, `/latest`, `/settings`, `/help`, `/summary`), убрать dev
- [x] 4.3 Обновить `app/main.py:_register_commands` — `BotCommand` только прод
- [x] 4.4 Добавить к приветствию кнопку `[🔍 Показать пример уведомления]` (`callback_data=show_example`) → хендлер берёт последний `FZ` (`level='FZ'` `ORDER BY published_at DESC LIMIT 1`, fallback любой) и шлёт через `_build_notification` как тестовое уведомление

## 5. Проверка

- [x] 5.1 `py_compile` всех изменённых модулей, `openspec validate --changes`, `rg` на dev-команды
- [x] 5.2 Ручная проверка: новый юзер `/start` → визард, `/latest` с дефолтом `[Конституция,ФКЗ,ФЗ]` + подсказка → 10 без саммари, `/settings` меняет, рестарт → нет спама, `429`/`403` handling, Конституция/ФКЗ/ФЗ без лимита
