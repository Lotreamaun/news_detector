## 1. Сервис счётчика

- [x] 1.1 Создать `app/services/summary_usage.py`: `current_month()`, dataclass `SummaryUsage` (`used`, `limit`, `unlimited`, `resets_on`), `get_summary_usage(config, session_maker, user)` (чтение `summarization_usage`, `used = min(count, limit)`, администратор → `unlimited`), `format_reset_date(date)` («1 ноября»), `limit_message(usage)`, `NO_SPEND_NOTE`
- [x] 1.2 В `app/services/force_summary.py` заменить `datetime.now().strftime("%Y-%m")` на `current_month()` (единственная правка в файле)
- [x] 1.3 Проверить функции одноразовым вызовом `python -c` (пустая таблица → `0/10`; граница декабря → «1 января»; клэмп `count > limit`; администратор → `unlimited`)

## 2. Бот

- [x] 2.1 В `level_wizard_entry` (`app/bot/handlers.py`) добавить строку счётчика к тексту `/settings`: обычный вид, вид «закончились», вид «без ограничений»; чтение обернуть в `try/except` (при сбое строка опускается)
- [x] 2.2 В `_summarize_and_reply` заменить текст `fallback` для `limit` на `limit_message(usage)` (получить `usage` после `run_force_summarize`); заголовок и ссылка сохраняются через `_deliver_summary_failure`
- [x] 2.3 Не трогать неиспользуемый `settings_command`

## 3. Сервер Mini-App

- [x] 3.1 Вынести проверку `initData` + загрузку пользователя + гейт канала из `handle_summarize` в хелпер `_auth_user(request, init_data)`; поведение `/summarize` не меняется (400/401/403 как раньше)
- [x] 3.2 Добавить `POST /usage` (`{init_data}` → `{used, limit, unlimited, resets_on}`), зарегистрировать в `create_app`, обновить docstring-шапку модуля со списком роутов
- [x] 3.3 В `handle_summarize` добавлять `usage` в ответ для всех доменных исходов (`ok`, `limit`, `no_text`, `refused`, `error`, `cooldown`); `messages["limit"]` = `limit_message(usage)`; для `no_text`/`refused`/`error`/`cooldown` дописывать `NO_SPEND_NOTE`, если `not usage.unlimited`

## 4. Страница Mini-App

- [x] 4.1 Разметка: блок `#usage` (чип-кнопка и плашка-пояснение) первым в `.container`, скрыт по умолчанию
- [x] 4.2 JS: состояние `usage`, `renderUsage()`, `loadUsage()` (только режимы `ids`/`today` и только при наличии `tg.initData`; ошибки молча), `setUsage()` из ответа `/summarize` во всех ветках, включая `limit` и `cooldown`
- [x] 4.3 JS: плашка-пояснение — открытие по клику на чип, закрытие повторным кликом / кликом вне / таймером ~8 с / при показе тоста; три варианта текста (есть попытки, закончились, без ограничений) с датой обновления
- [x] 4.4 CSS (`webapp.css`): чип, состояние `out` (палитра `.error`), плашка (палитра `.unavailable`), тёмные варианты, `max-width: calc(100vw - 32px)`, z-index плашки ниже тоста, `prefers-reduced-motion`
- [x] 4.5 Убедиться, что при отказе по лимиту секция получает сообщение на месте кнопки и тост, а кнопки в остальных секциях остаются

## 5. Проверка

- [x] 5.1 Тестовым клиентом aiohttp: `/usage` — 200 для известного пользователя, 401 без подписи/с неверной, 403 без подписки на канал; `/summarize` возвращает `usage` в исходах `ok`/`limit`/`cooldown`; готовое саммари и уровень ФЗ при `N/N` не списывают попытку
- [x] 5.2 В headless-браузере (по образцу проверки `miniapp-summarize-feedback`) с подставленным `Telegram.WebApp.initData`: чип `4/10`, плашка открывается/закрывается тремя способами, состояние `10/10`, отказ по лимиту (баннер + сообщение на месте кнопки + чип `out`), светлая и тёмная темы, ширина 320 px, одиночный просмотр и вне Telegram — без чипа
- [ ] 5.3 Вручную в Telegram: `/settings` у обычного пользователя и администратора; отказ по лимиту в боте содержит дату и подсказку; страница «сегодня» и дайджест показывают чип
- [x] 5.4 Обновить `CLAUDE.md` (разделы Mini-App и Key modules: `POST /usage`, `summary_usage.py`) и прогнать `openspec validate show-summary-limit --strict`
