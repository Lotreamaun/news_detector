## 1. Модели и таблицы (`app/models/`)

- [x] 1.1 `app/models/broadcast.py`: `Broadcast` (`id`, `created_at`, `created_by`, `text`, `options` — JSON-строка списка, `action_kind`, `action_label`, `action_url`, `segment`, `recipients`, `status` `sending`/`done`, `finished_at`, `client_token` unique), `BroadcastDelivery` (PK `broadcast_id` + `telegram_id`, `status` `pending`/`sent`/`blocked`/`error`, `sent_at`), `BroadcastAnswer` (PK `broadcast_id` + `telegram_id`, `option`, `answered_at`) — design.md, решение 1
- [x] 1.2 Экспорт в `app/models/__init__.py` (докстринг и `__all__`); проверить, что `init_db_schema()` на копии прод-бэкапа создаёт три таблицы и не трогает существующие

## 2. Доставка через `bot` (`app/services/scheduler.py`)

- [x] 2.1 `_send_notification` и `_deliver_notification` принимают `bot` вместо `context` (используется только `context.bot`); три вызова (обычная рассылка, недельная подборка, `_send_notification` → `_deliver_notification`) передают `context.bot`; докстринги обновить, поведение (429, 403, журнал) не меняется
- [x] 2.2 Проверить, что больше нигде (`grep`) эти функции не вызываются со старой сигнатурой

## 3. Журнал (`app/services/events.py`, `app/services/stats.py`)

- [x] 3.1 Константа `BROADCAST_STARTED = "broadcast.started"` в группе «Доставка»
- [x] 3.2 `stats._describe`: «📣 рассылка #<id> запущена · получателей: <N>» из `ref = "<id>:<N>"`; событие не в `FEED_EXCLUDED_KINDS`

## 4. Сервис рассылок (`app/services/broadcasts.py`)

- [x] 4.1 Константы сегментов (`subscribers`, `active_14d`, `silent_14d`, `unverified`) и подписи для панели; функция `segment_users(session, config, segment)` — запросы по design.md, решение 2: `ADMIN_CHAT_IDS` исключены, `active_14d` по `stats.ACTIVE_KINDS` за 14 дней, `silent_14d` — остальные подписчики, `unverified` только при `REQUIRED_CHANNEL_ID`
- [x] 4.2 `validate_draft(payload, config) -> (draft | ошибка)`: текст 1–3500 и ≤ 4096 после `escape_markdown(version=2)`, 0–4 варианта по 1–40 без повторов, одна кнопка действия (`link` с `https://` и подписью 1–40 / `check_subscription`), сегмент из списка — решение 7
- [x] 4.3 `build_markup(broadcast_id, options, action)`: варианты `bc:<id>:<n>` по одному в строке, отмеченный вариант с «✓ », кнопка действия последней (URL-кнопка или `check_subscription`); для теста `id = 0`
- [x] 4.4 `start_broadcast(session_maker, config, admin_id, draft, client_token)`: повтор `client_token` → существующая рассылка; есть рассылка в `sending` → отказ «идёт отправка»; ноль получателей → отказ; иначе одна транзакция: `Broadcast` + `pending`-строки по снимку сегмента; `record_event(BROADCAST_STARTED, ref="<id>:<N>")`
- [x] 4.5 `run_broadcast(bot, session_maker, broadcast_id)`: по `pending`-строкам — `_send_notification(..., kind="broadcast")` с экранированным текстом и разметкой, статус строки (`True` → `sent`, `None` → `blocked`, `False` → `error`) и `sent_at` с отдельным commit, пауза 0,05 с; в конце `status = done`, `finished_at`; исключение по одному получателю → `error` и дальше; задача в модульном `set`
- [x] 4.6 `spawn_broadcast(...)` (создать задачу и положить в `set`) и `resume_broadcasts(bot, session_maker)` — запуск `run_broadcast` для всех `sending`
- [x] 4.7 `send_test(bot, admin_id, draft)`: одно сообщение админу с `build_markup(0, …)`, без записи в БД
- [x] 4.8 `record_answer(session_maker, broadcast_id, telegram_id, option) -> options | None`: рассылка есть, `option` в пределах, есть строка доставки этому пользователю → upsert ответа и вернуть варианты для перерисовки; иначе `None`
- [x] 4.9 `list_broadcasts(session_maker, limit=20)`: новые сверху, по каждой — время, сегмент, начало текста, `recipients`, `status`, счётчики `sent/blocked/error/pending`, ответы по вариантам с долей от `sent`

## 5. Бот (`app/bot/handlers.py`, `app/main.py`)

- [x] 5.1 Обработчик `broadcast_answer` на `^bc:\d+:\d+$`: `id = 0` → `query.answer("Тестовая рассылка — ответ не сохраняется")`; иначе `record_answer`; при `None` — `query.answer()` без сохранения; при успехе — `query.answer("Спасибо!")` и `edit_message_reply_markup` с отмеченным вариантом, «Message is not modified» игнорируется
- [x] 5.2 Регистрация в `app/main.py` **без** `require_channel_verified`, рядом с allowlist онбординга; поправить комментарий об исключениях из гейта
- [x] 5.3 `post_init`: после старта WebApp вызвать `resume_broadcasts(application.bot, session_maker)`; сбой → `logger.exception`, старт не прерывается

## 6. API панели (`app/webapp/admin.py`)

- [x] 6.1 `GET /admin/api/broadcasts/audience?segment=` → `{count}`, неизвестный сегмент → 400
- [x] 6.2 `POST /admin/api/broadcasts/test` → `validate_draft` + `send_test` администратору из `require_admin`; ошибки ввода → 400 с текстом
- [x] 6.3 `POST /admin/api/broadcasts` → `validate_draft` + `start_broadcast` + `spawn_broadcast(request.app["bot"], …)`; ответ `{id, recipients}`; «идёт отправка» → 409, ноль получателей → 400
- [x] 6.4 `GET /admin/api/broadcasts` → `list_broadcasts` и флаг доступности сегмента `unverified`
- [x] 6.5 Маршруты в `setup_admin` без условия на `WEEKLY_DIGEST_TIME`; POST через существующие `require_admin` и `_read_body` (заголовок `X-Admin-Panel`, JSON)

## 7. Вкладка «Рассылки» (`app/webapp/static/admin.html`)

- [x] 7.1 Кнопка вкладки `data-tab="broadcasts"`, видна всегда, когда показаны вкладки панели; состояние «загрузка / ошибка» как у «Статистики»
- [x] 7.2 Форма: текст (счётчик символов), 4 поля вариантов, кнопка действия (нет / ссылка + подпись + URL / «Проверить подписку»), сегмент с числом получателей (запрос `audience` при смене); `client_token` генерируется при открытии формы и после успешного запуска
- [x] 7.3 «Отправить себе» → `test`; «Разослать N получателям» → первое нажатие меняет текст на «Нажмите ещё раз, чтобы разослать N», второе в течение 5 с отправляет; ошибки сервера показываются под формой; после запуска форма очищается
- [x] 7.4 Список рассылок: время по Москве, сегмент, начало текста, `отправлено / заблокировали / ошибка / в очереди`, по вариантам «N · X%» с полосой; пока есть `sending` — опрос каждые 3 с только при видимой странице
- [x] 7.5 `HINTS`: в пояснении плитки «Доставка» упомянуть, что считаются и рассылки администратора

## 8. Проверка и документация

- [x] 8.1 `python -c "from app.core.config import Config; print(Config.load())"`, импорт `app.main` и `app.services.broadcasts` проходят
- [x] 8.2 Локально (свой бот-токен, копия прод-бэкапа): тест себе — текст со спецсимволами виден как есть, ответ даёт «Тестовая рассылка»; рассылка на сегмент с собой в роли не-админа (временно убрать себя из `ADMIN_CHAT_IDS`) — ответ сохраняется, смена ответа заменяет его, ✓ переезжает, кнопка действия остаётся; неверные варианты/URL/длина отклоняются
- [x] 8.3 Двойной клик «Разослать» → одна рассылка; запуск второй во время первой → 409; рестарт процесса во время отправки (искусственная пауза) → отправка продолжается только `pending`
- [x] 8.4 Обычная проверка законов и `/test_digest weekly` после смены сигнатуры доставки работают как раньше; в Пульсе лента показывает запуск рассылки, плитка «Доставка» растёт
- [x] 8.5 Неподтверждённый пользователь (сегмент `unverified`) отвечает на опрос без приглашения подписаться; «Проверить подписку» из рассылки проходит обычный путь подтверждения
- [x] 8.6 CLAUDE.md: абзац про рассылки (вкладка, сегменты, однократная доставка с возобновлением, ответы `bc:<id>:<n>` вне гейта, одна рассылка одновременно), строки `app/services/broadcasts.py` и `app/models/broadcast.py` в таблице модулей, `broadcast.started` в «Event journal», `admin-broadcasts` в списке спек после архивации
